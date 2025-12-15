#!/usr/bin/env python3
import argparse
import subprocess
import os
import sys
import shutil
import urllib.request
import toml
import json
import time

# Configuration
DEFAULT_GENESIS_URL = "https://github.com/cosmos/testnets/raw/master/interchain-security/provider/provider-genesis.json"
DEFAULT_BINARY = "./build/gaiad"
DEFAULT_HOME = os.path.expanduser("~/.gaiad-testnet")
#SUPPORTED_BACKENDS = ["treemapgemini", "treedb", "gomap", "goleveldb", "pebbledb", "memdb"]
SUPPORTED_BACKENDS = ["gemini", "geminicached", "treedb", "gomap", "goleveldb", "pebbledb", "memdb"]
CHAIN_ID = "provider"
MIN_GAS_PRICE = "0.005stake"

# Provided SEEDS from the user (better than general peers for bootstrapping)
SEEDS = "08ec17e86dac67b9da70deb20177655495a55407@provider-seed-01.ics-testnet.polypore.xyz:26656,4ea6e56300a2f37b90e58de5ee27d1c9065cf871@provider-seed-02.ics-testnet.polypore.xyz:26656"

# Provided SYNC_RPC_SERVERS for state sync
SYNC_RPC_SERVERS = "https://rpc.provider-state-sync-01.ics-testnet.polypore.xyz:443,https://rpc.provider-state-sync-02.ics-testnet.polypore.xyz:443"

def run_command(cmd, shell=False, check=True):
    """Runs a shell command."""
    print(f"Running: {' '.join(cmd) if isinstance(cmd, list) else cmd}")
    try:
        subprocess.run(cmd, shell=shell, check=check, text=True)
    except subprocess.CalledProcessError as e:
        print(f"Error executing command: {e}")
        sys.exit(1)

def increment_port(addr, offset):
    """Increments the port in a 'tcp://host:port' or 'host:port' string."""
    if not addr or offset == 0:
        return addr
    
    prefix = ""
    if "://" in addr:
        prefix, addr = addr.split("://")
        prefix += "://"
        
    try:
        host, port = addr.rsplit(":", 1)
        new_port = int(port) + offset
        return f"{prefix}{host}:{new_port}"
    except ValueError:
        return f"{prefix}{addr}" # Return original if parsing fails

def get_trust_settings(rpc_servers_str):
    """Fetches trust height and hash from the first available RPC server."""
    rpc_servers = rpc_servers_str.split(',')
    
    for rpc in rpc_servers:
        try:
            print(f"Fetching trust settings from {rpc}...")
            # Get status to find latest height
            with urllib.request.urlopen(f"{rpc}/status") as response:
                status = json.loads(response.read().decode())
                latest_height = int(status['result']['sync_info']['latest_block_height'])
            
            # Use a height 2000 blocks back to be safe (must be within trusting period)
            trust_height = latest_height - 2000
            if trust_height <= 0:
                print(f"Chain too young ({latest_height}), skipping {rpc}")
                continue

            # Get hash for the trust height
            with urllib.request.urlopen(f"{rpc}/block?height={trust_height}") as response:
                block_data = json.loads(response.read().decode())
                trust_hash = block_data['result']['block_id']['hash']
            
            return trust_height, trust_hash
        except Exception as e:
            print(f"Failed to fetch trust settings from {rpc}: {e}")
    
    print("Could not fetch trust settings from any RPC server.")
    return None, None

def main():
    parser = argparse.ArgumentParser(description="Start a Gaia testnet node with a specific DB backend.")
    
    # Temporarily add "treedb" to choices to allow custom handling before argparse validation
    # This is a workaround as argparse validates choices before custom logic.
    all_supported_choices = SUPPORTED_BACKENDS # treedb is in SUPPORTED_BACKENDS
    parser.add_argument("--backend", type=str, required=True, choices=all_supported_choices,
                        help=f"Database backend to use. Supported: {', '.join(SUPPORTED_BACKENDS)}. 'treedb' will fall back to 'goleveldb'.")

    
    parser.add_argument("--binary", type=str, default=DEFAULT_BINARY,
                        help=f"Path to the gaiad binary (default: {DEFAULT_BINARY})")
    
    parser.add_argument("--home", type=str, default=DEFAULT_HOME,
                        help=f"Home directory for the node (default: {DEFAULT_HOME})")
    
    parser.add_argument("--genesis-url", type=str, default=DEFAULT_GENESIS_URL,
                        help="URL to the genesis file")
    
    parser.add_argument("--clean", action="store_true",
                        help="Wipe the home directory before starting")

    parser.add_argument("--moniker", type=str, default="test-node",
                        help="Moniker for the node")
    
    parser.add_argument("--state-sync-enable", action="store_true",
                        help="Enable state sync for faster node setup.")
    
    parser.add_argument("--state-sync-rpc-servers", type=str, default=SYNC_RPC_SERVERS,
                        help="Comma-separated list of RPC servers for state sync.")
                        
    parser.add_argument("--port-offset", type=int, default=1000,
                        help="Increment all listening ports by this amount to avoid conflicts.")
    
    parser.add_argument("--disable-fastnode", action="store_true",
                        help="Disable IAVL fast node optimization (useful if backends hang on upgrade).")
    
    parser.add_argument("--halt-height", type=int, default=0,
                        help="Block height at which to gracefully halt the chain and shutdown the node (0 for no halt).")

    args = parser.parse_args()

    # Check if binary exists
    if not os.path.isfile(args.binary):
        print(f"Error: Binary not found at {args.binary}")
        print("Please build it first (e.g., 'make build') or provide the correct path.")
        sys.exit(1)

    # Dynamic check for backend support
    def check_backend_support(binary_path, backend):
        try:
            # Run help command to get supported backends
            result = subprocess.run(
                [binary_path, "start", "--help"], 
                capture_output=True, 
                text=True, 
                check=False
            )
            # Look for the backend in the output
            # Output format is usually: --db_backend string database backend: goleveldb | cleveldb ...
            if "db_backend" in result.stdout:
                return backend in result.stdout
            return False
        except Exception as e:
            print(f"Warning: Could not verify backend support: {e}")
            return True # Assume supported if check fails to avoid blocking

    if args.backend == "treedb":
        if not check_backend_support(args.binary, "treedb"):
            print("Warning: 'treedb' does not appear in 'gaiad --help'. Proceeding with 'treedb' as requested (may fail if unsupported).")
            # args.backend = "goleveldb" # Allow user to try treedb even if check fails
        else:
            print("Confirmed 'treedb' support in gaiad binary.")

    # Resolve home directory
    args.home = os.path.expanduser(args.home)

    # Clean home directory if requested
    if args.clean:
        print("Cleaning requested. Checking for running gaiad processes...")
        try:
            # Kill running gaiad processes
            subprocess.run(["pkill", "gaiad"], check=False)
            time.sleep(3) # Give it a moment to terminate
        except Exception as e:
            print(f"Warning: Failed to kill gaiad processes: {e}")

        if os.path.exists(args.home):
            print(f"Cleaning home directory: {args.home}")
            shutil.rmtree(args.home)

    # Initialize node if config doesn't exist
    config_dir = os.path.join(args.home, "config")
    config_path = os.path.join(config_dir, "config.toml")
    app_config_path = os.path.join(config_dir, "app.toml")
    genesis_path = os.path.join(config_dir, "genesis.json")

    # If the home directory does not exist or we are cleaning, re-initialize
    if not os.path.exists(config_dir) or args.clean:
        print(f"Initializing node at {args.home}...")
        run_command([args.binary, "init", args.moniker, "--chain-id", CHAIN_ID, "--home", args.home])
        
        # Local Testnet Initialization (Replacing broken genesis download)
        print("Configuring local testnet...")
        
        # Create a validator key
        run_command([args.binary, "keys", "add", "validator", "--home", args.home, "--keyring-backend", "test", "--output", "json"])
        
        # Add genesis account
        run_command([args.binary, "genesis", "add-genesis-account", "validator", "1000000000stake", "--home", args.home, "--keyring-backend", "test"])
        
        # Generate gentx
        run_command([args.binary, "genesis", "gentx", "validator", "100000000stake", "--chain-id", CHAIN_ID, "--home", args.home, "--keyring-backend", "test"])
        
        # Collect gentxs
        run_command([args.binary, "genesis", "collect-gentxs", "--home", args.home])
        
        # Configure config.toml
        print("Configuring config.toml...")
        # Load as string first to handle potential comments or formatting not handled by toml.load directly
        with open(config_path, 'r') as f:
            config_lines = f.readlines()

        config_data = toml.load(config_path) # Load again for structured modification

        # Set seeds
        config_data['p2p']['seeds'] = SEEDS
        config_data['p2p']['persistent_peers'] = "" # Clear persistent_peers if seeds are used
        
        # Set DB backend in config (though flag usually overrides, it's good practice)
        config_data['db_backend'] = args.backend
        
        # Apply port offset to config.toml
        if args.port_offset > 0:
            print(f"Applying port offset of {args.port_offset} to config.toml...")
            config_data['rpc']['laddr'] = increment_port(config_data['rpc']['laddr'], args.port_offset)
            config_data['rpc']['pprof_laddr'] = increment_port(config_data['rpc']['pprof_laddr'], args.port_offset)
            config_data['p2p']['laddr'] = increment_port(config_data['p2p']['laddr'], args.port_offset)

        # Configure State Sync if enabled
        if args.state_sync_enable:
            print("Fetching trust height and hash for state sync...")
            trust_height, trust_hash = get_trust_settings(args.state_sync_rpc_servers)
            
            if trust_height and trust_hash:
                config_data['statesync']['enable'] = True
                config_data['statesync']['rpc_servers'] = args.state_sync_rpc_servers
                config_data['statesync']['trust_height'] = trust_height
                config_data['statesync']['trust_hash'] = trust_hash
                config_data['statesync']['trust_period'] = "168h" # 7 days
                print(f"State sync enabled. Trust Height: {trust_height}, Trust Hash: {trust_hash}")
            else:
                print("Warning: Could not enable state sync due to missing trust settings.")
                config_data['statesync']['enable'] = False
        else:
            config_data['statesync']['enable'] = False
            config_data['statesync']['rpc_servers'] = "" # Clear if not using state sync

        # Write the modified config back
        with open(config_path, 'w') as f:
            toml.dump(config_data, f)

        # Configure app.toml
        print(f"Configuring app.toml with min-gas-prices: {MIN_GAS_PRICE}...")
        app_data = toml.load(app_config_path)
        
        app_data['minimum-gas-prices'] = MIN_GAS_PRICE
        
        # Apply port offset to app.toml
        if args.port_offset > 0:
            print(f"Applying port offset of {args.port_offset} to app.toml...")
            if 'api' in app_data:
                app_data['api']['address'] = increment_port(app_data['api'].get('address', 'tcp://0.0.0.0:1317'), args.port_offset)
            if 'grpc' in app_data:
                app_data['grpc']['address'] = increment_port(app_data['grpc'].get('address', '0.0.0.0:9090'), args.port_offset)
            if 'grpc-web' in app_data:
                 app_data['grpc-web']['address'] = increment_port(app_data['grpc-web'].get('address', '0.0.0.0:9091'), args.port_offset)
        
        with open(app_config_path, 'w') as f:
            toml.dump(app_data, f)
            
        print("Configuration complete.")

    else:
        print(f"Node already initialized at {args.home}. Skipping init.")
        # Ensure the selected backend is reflected in config.toml for consistency, even if not re-initializing
        config_data = toml.load(config_path)
        if config_data['db_backend'] != args.backend:
            config_data['db_backend'] = args.backend
            with open(config_path, 'w') as f:
                toml.dump(config_data, f)
            print(f"Updated config.toml to use backend: {args.backend}")


    # Start the node
    print(f"Starting node with backend: {args.backend}")
    start_cmd = [
        args.binary, "start",
        "--home", args.home,
        "--db_backend", args.backend,
    ]
    
    if args.disable_fastnode:
        print("Disabling IAVL fast node...")
        start_cmd.append("--iavl-disable-fastnode")

    if args.halt_height > 0:
        print(f"Setting halt height to {args.halt_height}...")
        start_cmd.append(f"--halt-height={args.halt_height}")
    
    # Check if we are state syncing, if so we might need unsafe-reset-all (if not already done by init/previous run)
    if args.state_sync_enable and (not os.path.exists(os.path.join(args.home, "data", "priv_validator_state.json"))):
        print("Performing unsafe-reset-all for state sync preparation...")
        run_command([args.binary, "tendermint", "unsafe-reset-all", "--home", args.home])

    try:
        run_command(start_cmd)
    except KeyboardInterrupt:
        print("\nNode stopped.")

if __name__ == "__main__":
    main()
