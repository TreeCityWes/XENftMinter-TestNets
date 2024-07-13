import csv
import base64
import json
from datetime import datetime, timezone
from web3 import Web3, HTTPProvider
import getpass
import time

# Function to decode mintInfo
def decode_mint_info(encoded_info):
    term = (encoded_info >> 240) & 0xFFFF
    maturity_ts = (encoded_info >> 176) & 0xFFFFFFFFFFFFFFFF
    rank = (encoded_info >> 48) & 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF
    amp = (encoded_info >> 32) & 0xFFFF
    eaa = (encoded_info >> 16) & 0xFFFF
    class_byte = (encoded_info >> 8) & 0xFF
    redeemed = encoded_info & 0xFF

    class_ = class_byte & 0x3F
    apex = (class_byte & 0x80) > 0
    limited = (class_byte & 0x40) > 0

    return {
        "term": term,
        "maturityTs": maturity_ts,
        "rank": rank,
        "amp": amp,
        "eaa": eaa,
        "class": class_,
        "apex": apex,
        "limited": limited,
        "redeemed": redeemed == 1
    }

# Function to decode token URI and extract data
def decode_token_uri(token_uri):
    _, encoded = token_uri.split(",", 1)
    decoded_json = base64.b64decode(encoded).decode("utf-8")
    token_data = json.loads(decoded_json)
    return {
        "term": token_data["attributes"][8]["value"],
        "due_date": token_data["attributes"][7]["value"],
        "redeemed": token_data.get("redeemed", False) # Assuming "redeemed" field is in the token URI
    }

def get_owned_tokens(contract, account_address, retries=3, delay=5):
    for attempt in range(retries):
        try:
            return contract.functions.ownedTokens().call({'from': account_address})
        except Exception as e:
            print(f"Attempt {attempt+1} failed with error: {e}")
            if attempt < retries - 1:
                time.sleep(delay)
    raise Exception("All attempts to fetch owned tokens failed")

# Function to check if the token is due for claiming
def is_token_due(due_date_str):
    due_date = datetime.strptime(due_date_str, "%b %d, %Y %H:%M %Z").replace(tzinfo=timezone.utc)
    return due_date <= datetime.utcnow().replace(tzinfo=timezone.utc)

def claim_mint_rewards(w3, contract, contract_address, nft_id, account_address, private_key, redeemed_nfts):
    try:
        if nft_id in redeemed_nfts:
            print(f"NFT {nft_id} has already been redeemed.")
            return False

        # Fetch and decode token URI
        token_uri = contract.functions.tokenURI(nft_id).call()
        token_info = decode_token_uri(token_uri)

        if token_info.get('redeemed', False):
            print(f"NFT {nft_id} has already been redeemed according to token URI.")
            return False

        due_date_str = token_info['due_date']
        if not is_token_due(due_date_str):
            print(f"NFT {nft_id} is not due for claiming yet.")
            return False

        # Check MintInfo for redemption status
        mint_info_encoded = contract.functions.mintInfo(nft_id).call()
        mint_info = decode_mint_info(mint_info_encoded)

        # Display the mint info
        print(f"Decoded MintInfo for NFT {nft_id}: {mint_info}")

        if mint_info['redeemed']:
            print(f"NFT {nft_id} has already been redeemed according to MintInfo.")
            return False

        # Build the transaction data for bulkClaimMintReward
        function_call_data = contract.encodeABI(
            fn_name='bulkClaimMintReward',
            args=[nft_id, account_address]
        )

        gas_limit = 7000000  # Set gas limit to 7 million

        while True:
            try:
                # Get estimated gas price from the node
                gas_price = w3.eth.gas_price
                nonce = w3.eth.get_transaction_count(account_address)  # Fetch nonce within the loop

                transaction = {
                    'from': account_address,
                    'to': contract_address,
                    'nonce': nonce,
                    'gasPrice': gas_price,
                    'gas': gas_limit,
                    'data': function_call_data,
                    'chainId': 4003  # Correct chain ID
                }

                signed_txn = w3.eth.account.sign_transaction(transaction, private_key=private_key)
                txn_hash = w3.eth.send_raw_transaction(signed_txn.rawTransaction)

                # Wait for transaction confirmation
                if wait_for_transaction(w3, txn_hash):
                    print(f"Successfully claimed reward for NFT {nft_id}")
                    redeemed_nfts.add(nft_id)  # Add the redeemed NFT to the set
                    return True
                else:
                    print(f"Failed to claim reward for NFT {nft_id}")
                    return False
            except ValueError as e:
                # Handle potential errors like nonce too low
                print(f"Error sending transaction: {e}")
                nonce = w3.eth.get_transaction_count(account_address)  # Update nonce
            except Exception as e:
                # Handle other errors
                print(f"Error claiming NFT {nft_id}: {e}")
                return False
    except Exception as e:
        print(f"Error claiming NFT {nft_id}: {e}")
        return False


def wait_for_transaction(w3, txn_hash, timeout=120):
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            receipt = w3.eth.get_transaction_receipt(txn_hash)
            if receipt is not None:
                if receipt.status == 1:
                    return True
                else:
                    return False
        except Exception as e:
            print(f"Error checking transaction receipt: {e}")
        time.sleep(5) # Poll every 5 seconds
    return False

def main():
    print("Welcome to the XENFT Claiming Tool")

    # User inputs
    account_address = input("Enter your Ethereum account address: ")
    private_key = getpass.getpass("Enter your private key: ")

    # Setup Web3
    contract_address = "0xd638e3657a4000b944AC517BD3aFe2Ba964E3B92"
    eth_node_url = "https://x1-fastnet.infrafc.org"
    w3 = Web3(HTTPProvider(eth_node_url))
    with open('xenftABI.json', 'r') as abi_file:
        contract_abi = json.load(abi_file)
    contract = w3.eth.contract(address=contract_address, abi=contract_abi)

    # Fetch owned tokens with retry
    try:
        owned_nfts = get_owned_tokens(contract, account_address)
        print(f"Total owned NFTs: {len(owned_nfts)}")
    except Exception as e:
        print(f"Failed to fetch owned tokens: {e}")
        return

    # CSV File setup
    csv_filename = 'xenft_data.csv'
    csv_headers = ['TokenID', 'Term', 'DueDate']

    # Initialize CSV
    existing_data = {}
    try:
        with open(csv_filename, mode='r', newline='') as file:
            reader = csv.DictReader(file)
            for row in reader:
                existing_data[int(row['TokenID'])] = row
    except FileNotFoundError:
        with open(csv_filename, mode='w', newline='') as file:
            writer = csv.DictWriter(file, fieldnames=csv_headers)
            writer.writeheader()

    # Initialize counters
    claimed_count = 0
    due_count = 0

    # Initialize a set to keep track of redeemed NFTs
    redeemed_nfts = set()

    # Process each NFT
    for index, nft_id in enumerate(reversed(owned_nfts)):
        print(f"Processing NFT {index+1}/{len(owned_nfts)}: Token ID {nft_id}")

        # Fetch and decode token URI
        if nft_id not in existing_data:
            token_uri = contract.functions.tokenURI(nft_id).call()
            token_info = decode_token_uri(token_uri)
            existing_data[nft_id] = {
                'TokenID': nft_id,
                'Term': token_info['term'],
                'DueDate': token_info['due_date']
            }

            # Update CSV with new data
            with open(csv_filename, mode='a', newline='') as file:
                writer = csv.DictWriter(file, fieldnames=csv_headers)
                writer.writerow(existing_data[nft_id])

        # Check if due
        try:
            if is_token_due(existing_data[nft_id]['DueDate']):
                due_count += 1

                # Claim logic here
                if claim_mint_rewards(w3, contract, contract_address, nft_id, account_address, private_key, redeemed_nfts):
                    claimed_count += 1
                else:
                    print(f"Failed to claim reward for NFT {nft_id}")

                # Add a delimiter line after each transaction
                print("______________________________________________________________")
            else:
                print(f"NFT {nft_id} is not due for claiming yet.")
        except ValueError as e:
            print(f"Error with date format for NFT {nft_id}: {e}")
            continue  # Skip to the next NFT

    print("______________________________________________________________")  # Add a delimiter after all transactions
    print(f"Total NFTs due for claiming: {due_count}")
    print(f"Total NFTs successfully claimed: {claimed_count}")

if __name__ == "__main__":
    main()
