#! /usr/bin/env python3

# Hardware wallet interaction script

import argparse
import binascii
import hid
import json
import sys
import logging

from .device_ids import trezor_device_ids, keepkey_device_ids, ledger_device_ids,\
                        digitalbitbox_device_ids, coldcard_device_ids
from .serializations import PSBT, Base64ToHex, HexToBase64, hash160
from .base58 import xpub_to_address, xpub_to_pub_hex, get_xpub_fingerprint_as_id, get_xpub_fingerprint_hex, decompose_xpub, pubkey_to_address
from .bip32 import CKDpub

# Error codes
NO_DEVICE_PATH = -1
NO_DEVICE_TYPE = -2
DEVICE_CONN_ERROR = -3
UNKNWON_DEVICE_TYPE = -4
INVALID_TX = -5
NO_PASSWORD = -6
BAD_ARGUMENT = -7

# Get the client for the device
def get_client(device_type, device_path, password=None):
    # Open the device
    try:
        device = hid.device()
        device_path = bytes(device_path.encode())
        device.open_path(device_path)
    except Exception as e:
        print(e)
        return {'error':'Unable to connect to specified device','code':DEVICE_CONN_ERROR}

    # Make a client
    if device_type == 'trezor':
        from . import trezori
        client = trezori.TrezorClient(device=device, path=device_path)
    elif device_type == 'keepkey':
        from . import keepkeyi
        client = keepkeyi.KeepKeyClient(device=device, path=device_path)
    elif device_type == 'ledger':
        from . import ledgeri
        client = ledgeri.LedgerClient(device=device)
    elif device_type == 'digitalbitbox':
        if not password:
            return {'error':'Password must be supplied for digital BitBox','code':NO_PASSWORD}
        from . import digitalbitboxi
        client = digitalbitboxi.DigitalBitboxClient(device=device, password=password)
    elif device_type == 'coldcard':
        from . import coldcardi
        client = coldcardi.ColdCardClient(device=device)
    else:
        return {'error':'Unknown device type specified','code':UNKNWON_DEVICE_TYPE}
    return client

# Get a list of all available hardware wallets
def enumerate(args):
    result = []
    devices = hid.enumerate()
    for d in devices:
        d_data = {}
        # Get trezors
        if (d['vendor_id'], d['product_id']) in trezor_device_ids:
            d_data['type'] = 'trezor'
        # Get keepkeys
        elif (d['vendor_id'], d['product_id']) in keepkey_device_ids:
            d_data['type'] = 'keepkey'
        # Get ledgers
        elif (d['vendor_id'], d['product_id']) in ledger_device_ids:
            d_data['type'] = 'ledger'
        # Get DigitalBitboxes
        elif (d['vendor_id'], d['product_id']) in digitalbitbox_device_ids:
            d_data['type'] = 'digitalbitbox'
        # Get ColdCards
        elif (d['vendor_id'], d['product_id']) in coldcard_device_ids:
            d_data['type'] = 'coldcard'
        else:
            continue
        d_data['path'] = d['path'].decode("utf-8")
        d_data['serial_number'] = d['serial_number']

        try:
            client = get_client(d_data['type'], d_data['path'], args.password)
            master_xpub = client.get_pubkey_at_path('m/0h')['xpub']
            d_data['fingerprint'] = get_xpub_fingerprint_hex(master_xpub)
            client.close()
        except Exception as e:
            d_data['error'] = "Could not open client or get fingerprint information: " + str(e)
            pass

        result.append(d_data)
    return result

# Fingerprint or device type required
def find_device(args):
    assert(args.device_path is None)
    devices = enumerate(args)
    for d in devices:
        if args.device_type is not None and d['type'] != args.device_type:
            continue
        try:
            client = get_client(d['type'], d['path'], args.password)
            master_xpub = client.get_pubkey_at_path('m/0h')['xpub']
            master_fpr = get_xpub_fingerprint_hex(master_xpub)
            if args.fingerprint and master_fpr != args.fingerprint:
                client.close()
                continue
            else:
                return client
        except:
            pass # Ignore things we wouldn't get fingerprints for
    return None

def getmasterxpub(args, client):
    return client.get_master_xpub()

def signtx(args, client):
    # Deserialize the transaction
    try:
        tx = PSBT()
        tx.deserialize(args.psbt)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {'error':'You must provide a PSBT','code':INVALID_TX}
    return client.sign_tx(tx)

def getxpub(args, client):
    return client.get_pubkey_at_path(args.path)

def signmessage(args, client):
    return client.sign_message(args.message, args.path)

def getkeypool(args, client):
    # args[0]: path base (e.g. m/44'/0'/0')
    # args[1]; start index (e.g. 0)
    # args[2]: end index (e.g. 1000)
    # args[3]: internal (e.g. False)
    path_base = args.path_base
    start = args.start
    end = args.end
    internal = args.internal
    keypool = args.keypool

    master_xpub = client.get_pubkey_at_path('m/0h')['xpub']
    master_fpr = get_xpub_fingerprint_as_id(master_xpub)

    # Get the key at the base
    base_key = client.get_pubkey_at_path(path_base)['xpub']
    parent_pk, parent_cc = decompose_xpub(base_key)

    import_data = []
    for i in range(start, end + 1):
        this_import = {}
        if (path_base[-1] == '/'):
            path = path_base + str(i)
        else:
            path = path_base + '/' + str(i)

        child_pk, child_cc = CKDpub(parent_pk, parent_cc, i)
        address = pubkey_to_address(child_pk, args.testnet)

        this_import['pubkeys'] = [{binascii.hexlify(child_pk).decode() : {master_fpr : path.replace('\'', 'h')}}]
        this_import['scriptPubKey'] = {'address' : address}
        this_import['timestamp'] = 'now'
        this_import['internal'] = internal
        this_import['keypool'] = keypool
        import_data.append(this_import)
    return import_data

def displayaddress(args, client):
    if args.p2sh_p2wpkh == True and args.bech32 == True:
        return json.dumps({'error':'You must choose one address type only.','code':BAD_ARGUMENT})
    return client.display_address(args.path, args.p2sh_p2wpkh, args.bech32)

def process_commands(args):
    parser = argparse.ArgumentParser(description='Access and send commands to a hardware wallet device. Responses are in JSON format')
    parser.add_argument('--device-path', '-d', help='Specify the device path of the device to connect to')
    parser.add_argument('--device-type', '-t', help='Specify the type of device that will be connected')
    parser.add_argument('--password', '-p', help='Device password if it has one (e.g. DigitalBitbox)')
    parser.add_argument('--testnet', help='Use testnet prefixes', action='store_true')
    parser.add_argument('--debug', help='Print debug statements', action='store_true')
    parser.add_argument('--fingerprint', '-f', help='The first 4 bytes of the hash160 of the master public key')

    subparsers = parser.add_subparsers(description='Commands', dest='command')

    enumerate_parser = subparsers.add_parser('enumerate', help='List all available devices')
    enumerate_parser.set_defaults(func=enumerate)

    getmasterxpub_parser = subparsers.add_parser('getmasterxpub', help='Get the extended public key at m/44\'/0\'/0\'')
    getmasterxpub_parser.set_defaults(func=getmasterxpub)

    signtx_parser = subparsers.add_parser('signtx', help='Sign a PSBT')
    signtx_parser.add_argument('psbt', help='The Partially Signed Bitcoin Transaction to sign')
    signtx_parser.set_defaults(func=signtx)

    getxpub_parser = subparsers.add_parser('getxpub', help='Get an extended public key')
    getxpub_parser.add_argument('path', help='The BIP 32 derivation path to derive the key at')
    getxpub_parser.set_defaults(func=getxpub)

    signmsg_parser = subparsers.add_parser('signmessage', help='Sign a message')
    signmsg_parser.add_argument('message', help='The message to sign')
    signmsg_parser.add_argument('path', help='The BIP 32 derivation path of the key to sign the message with')
    signmsg_parser.set_defaults(func=signmessage)

    getkeypol_parser = subparsers.add_parser('getkeypool', help='Get JSON array of keys that can be imported to Bitcoin Core with importmulti')
    getkeypol_parser.add_argument('--internal', action='store_true', help='Indicates that the keys are change keys')
    getkeypol_parser.add_argument('--keypool', action='store_true', help='Indicates that the keys are to be imported to the keypool')
    getkeypol_parser.add_argument('path_base', help='The prefix of the derivation path')
    getkeypol_parser.add_argument('start', type=int, help='The index to start at. The first key will be <path_base>/<start>')
    getkeypol_parser.add_argument('end', type=int, help='The index to end at. The last key will be <path_base>/<end>')
    getkeypol_parser.set_defaults(func=getkeypool)

    displayaddr_parser = subparsers.add_parser('displayaddress', help='Display an address')
    displayaddr_parser.add_argument('path', help='The BIP 32 derivation path of the key embedded in the address')
    displayaddr_parser.add_argument('--p2sh_p2wpkh', action='store_true', help='Display the p2sh-nested segwit address associated with this key path')
    displayaddr_parser.add_argument('--bech32', action='store_true', help='Display the bech32 version of the address associated with this key path')
    displayaddr_parser.set_defaults(func=displayaddress)

    args = parser.parse_args(args)

    device_path = args.device_path
    device_type = args.device_type
    password = args.password
    command = args.command

    # Setup debug logging
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)

    # List all available hardware wallet devices
    if command == 'enumerate':
        return args.func(args)

    # Auto detect if we are using fingerprint or type to identify device
    if args.fingerprint or (args.device_type and not args.device_path):
        client = find_device(args)
        if not client:
            return {'error':'Could not find device with specified fingerprint','code':DEVICE_CONN_ERROR}
    else:
        return {'error':'You must specify a device type or fingerprint for all commands except enumerate','code':NO_DEVICE_PATH}

        client = get_client(device_type, device_path, password)
    client.is_testnet = args.testnet

    # Do the commands
    result = args.func(args, client)

    # Close the device
    client.close()

    return result
