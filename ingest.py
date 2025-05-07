import os
import argparse
from azure.identity import ClientSecretCredential
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.network import NetworkManagementClient
from netboxlabs.diode.sdk import DiodeClient
from netboxlabs.diode.sdk.ingester import (
    Entity,
    VirtualMachine,
    VirtualDisk,
    VMInterface,
    IPAddress,
    Cluster,
    ClusterGroup,
    ClusterType,
    Device,
    DeviceType,
    Manufacturer,
    Platform,
    Role,
    Site,
)

# Azure Authentication Parameters
tenant_id = os.environ.get("AZURE_TENANT_ID")
client_id = os.environ.get("AZURE_CLIENT_ID")
client_secret = os.environ.get("AZURE_CLIENT_SECRET")
subscription_id = os.environ.get("AZURE_SUBSCRIPTION_ID")

# NetBox Diode SDK Authentication
diode_target = os.environ.get("DIODE_TARGET", "grpc://localhost:8080/diode")
diode_client_id = os.environ.get("DIODE_CLIENT_ID")
diode_client_secret = os.environ.get("DIODE_CLIENT_SECRET")

def parse_args():
    parser = argparse.ArgumentParser(description='Ingest Azure VM data into NetBox')
    parser.add_argument('--debug', action='store_true', help='Enable debug output')
    parser.add_argument('--quiet', action='store_true', help='Suppress all non-error output')
    return parser.parse_args()

def get_azure_vms():
    """Authenticate to Azure and retrieve VM information"""
    # Authenticate to Azure
    credential = ClientSecretCredential(
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret
    )

    # Create Compute Management Client
    compute_client = ComputeManagementClient(credential, subscription_id)

    # Create Network Management Client
    network_client = NetworkManagementClient(credential, subscription_id)

    # Cache for subnet information to avoid repeated API calls
    subnet_cache = {}

    # Get all VMs in the subscription
    vms = compute_client.virtual_machines.list_all()

    vm_data = []
    for vm in vms:
        # Extract resource group from VM ID
        resource_group = vm.id.split('/')[4]

        # Get VM details
        vm_details = compute_client.virtual_machines.get(resource_group, vm.name, expand='instanceView')

        # Determine VM status
        status = "Unknown"
        if vm_details.instance_view and vm_details.instance_view.statuses:
            for vm_status in vm_details.instance_view.statuses:
                if vm_status.code.startswith('PowerState/'):
                    status = vm_status.code.split('/')[-1]
                    break

        # Get disk information
        disks = []

        # OS Disk
        os_disk = {
            'name': vm.storage_profile.os_disk.name,
            'size_gb': None,  # Will be populated if available
            'is_os_disk': True
        }

        # Data Disks
        data_disks = []
        for data_disk in vm.storage_profile.data_disks:
            data_disks.append({
                'name': data_disk.name,
                'size_gb': data_disk.disk_size_gb,
                'is_os_disk': False
            })

        # Try to get OS disk size
        try:
            os_disk_resource = compute_client.disks.get(resource_group, os_disk['name'])
            os_disk['size_gb'] = os_disk_resource.disk_size_gb
        except Exception as e:
            print(f"Warning: Could not get OS disk size for {vm.name}: {e}")

        # Combine OS disk and data disks
        disks = [os_disk] + data_disks

        # Get network interface information
        network_interfaces = []
        if vm.network_profile and vm.network_profile.network_interfaces:
            for nic_ref in vm.network_profile.network_interfaces:
                nic_id = nic_ref.id
                nic_name = nic_id.split('/')[-1]
                nic_resource_group = nic_id.split('/')[4]

                try:
                    # Get network interface details
                    nic = network_client.network_interfaces.get(nic_resource_group, nic_name)

                    # Get IP configurations
                    ip_configs = []
                    for ip_config in nic.ip_configurations:
                        ip_data = {
                            'name': ip_config.name,
                            'private_ip': ip_config.private_ip_address,
                            'private_ip_allocation': ip_config.private_ip_allocation_method,
                            'public_ip': None,
                            'subnet': None,
                            'subnet_prefix': None
                        }

                        # Get subnet information if available
                        if ip_config.subnet:
                            subnet_id = ip_config.subnet.id
                            ip_data['subnet'] = subnet_id.split('/')[-1]

                            # Check if we already have this subnet in our cache
                            if subnet_id in subnet_cache:
                                ip_data['subnet_prefix'] = subnet_cache[subnet_id]
                            else:
                                # Parse subnet ID to get resource group, vnet name, and subnet name
                                subnet_parts = subnet_id.split('/')
                                subnet_resource_group = subnet_parts[4]
                                vnet_name = subnet_parts[8]
                                subnet_name = subnet_parts[10]

                                try:
                                    # Get subnet details
                                    subnet = network_client.subnets.get(
                                        subnet_resource_group,
                                        vnet_name,
                                        subnet_name
                                    )

                                    # Extract address prefix
                                    ip_data['subnet_prefix'] = subnet.address_prefix

                                    # Cache the subnet prefix for future use
                                    subnet_cache[subnet_id] = subnet.address_prefix

                                except Exception as e:
                                    print(f"Warning: Could not get subnet details for {subnet_id}: {e}")

                        # Get public IP if available
                        if ip_config.public_ip_address:
                            public_ip_id = ip_config.public_ip_address.id
                            public_ip_name = public_ip_id.split('/')[-1]
                            public_ip_resource_group = public_ip_id.split('/')[4]

                            try:
                                public_ip = network_client.public_ip_addresses.get(
                                    public_ip_resource_group, 
                                    public_ip_name
                                )
                                ip_data['public_ip'] = public_ip.ip_address
                            except Exception as e:
                                print(f"Warning: Could not get public IP for {vm.name}, NIC {nic_name}: {e}")

                        ip_configs.append(ip_data)

                    # Add NIC with its IP configurations
                    network_interfaces.append({
                        'name': nic_name,
                        'id': nic_id,
                        'primary': nic_ref.primary if hasattr(nic_ref, 'primary') else False,
                        'ip_configurations': ip_configs,
                        'enabled': True
                    })

                except Exception as e:
                    print(f"Warning: Could not get network interface details for {vm.name}, NIC {nic_name}: {e}")
                    network_interfaces.append({
                        'name': nic_name,
                        'id': nic_id,
                        'primary': False,
                        'ip_configurations': [],
                        'enabled': False
                    })

        vm_data.append({
            'name': vm.name,
            'id': vm.id,
            'location': vm.location,
            'vm_size': vm.hardware_profile.vm_size,
            'os_type': vm.storage_profile.os_disk.os_type,
            'resource_group': resource_group,
            'status': status,
            'vcpus': None,  # Will be populated if available
            'memory_mb': None,  # Will be populated if available
            'disks': disks,
            'network_interfaces': network_interfaces
        })

        # Try to get VM size details to populate CPU, memory, and disk info
        try:
            vm_sizes = compute_client.virtual_machine_sizes.list(vm.location)
            for size in vm_sizes:
                if size.name == vm.hardware_profile.vm_size:
                    vm_data[-1]['vcpus'] = size.number_of_cores
                    vm_data[-1]['memory_mb'] = size.memory_in_mb
                    vm_data[-1]['disk_gb'] = size.os_disk_size_in_mb / 1024  # Convert to GB
                    break
        except Exception as e:
            print(f"Warning: Could not get VM size details for {vm.name}: {e}")

    return vm_data

def map_azure_status_to_netbox(azure_status):
    """Map Azure VM status to NetBox VM status"""
    status_map = {
        'running': 'active',
        'starting': 'staging',
        'stopping': 'decommissioning',
        'stopped': 'offline',
        'deallocating': 'decommissioning',
        'deallocated': 'offline'
    }

    # Default to offline if status is unknown
    return status_map.get(azure_status.lower(), 'offline')

def get_ip_with_prefix(ip_address, subnet_prefix):
    """
    Combine IP address with subnet prefix to create CIDR notation.
    If subnet_prefix is not available, default to /32 for IPv4 or /128 for IPv6.
    """
    if not ip_address:
        return None

    if subnet_prefix:
        # Extract just the prefix length from CIDR notation (e.g., "10.0.0.0/24" -> "24")
        prefix_length = subnet_prefix.split('/')[-1]
        return f"{ip_address}/{prefix_length}"

    # Default to /32 for IPv4 or /128 for IPv6
    if ':' in ip_address:  # IPv6
        return f"{ip_address}/128"
    else:  # IPv4
        return f"{ip_address}/32"

def ingest_to_netbox(vm_data, debug=False, quiet=False):
    """Ingest VM data into NetBox using Diode SDK"""
    # Print debug information about VM data
    if debug:
        print("\n=== DEBUG: VM Data ===")
        for i, vm in enumerate(vm_data):
            print(f"\nVM {i+1}: {vm['name']}")
            print(f" Location: {vm['location']}")
            print(f" Resource Group: {vm['resource_group']}")
            print(f" VM Size: {vm['vm_size']}")
            print(f" OS Type: {vm['os_type']}")
            print(f" Status: {vm['status']}")
            print(f" vCPUs: {vm['vcpus']}")
            print(f" Memory (MB): {vm['memory_mb']}")
            print(" Disks:")
            for disk in vm['disks']:
                print(f" - {disk['name']} ({'OS' if disk['is_os_disk'] else 'Data'}, Size: {disk['size_gb']} GB)")
            print(" Network Interfaces:")
            for nic in vm['network_interfaces']:
                print(f" - {nic['name']} (Primary: {nic['primary']})")
                for ip_config in nic['ip_configurations']:
                    print(f" - {ip_config['name']}: Private IP: {ip_config['private_ip']} (Subnet: {ip_config['subnet_prefix']}), Public IP: {ip_config['public_ip']}")

    # Initialize Diode client
    diode_client = DiodeClient(
        target=diode_target,
        app_name="azure-vm-ingest",
        app_version="1.0.0",
        client_id=diode_client_id,
        client_secret=diode_client_secret
    )

    try:
        # Step 1: Create and ingest ClusterType first
        cluster_type = ClusterType(
            name="Azure",
            description="Azure Virtual Machine Clusters"
        )

        cluster_type_entity = Entity(cluster_type=cluster_type)

        if not quiet:
            print("\nIngesting ClusterType...")
        cluster_type_response = diode_client.ingest(entities=[cluster_type_entity])
        if cluster_type_response.errors:
            print(f"Errors during ClusterType ingestion: {cluster_type_response.errors}")
        else:
            print("Successfully ingested ClusterType")

        # Step 2: Create and ingest ClusterGroup
        cluster_group = ClusterGroup(
            name="Azure",
            description="Azure Virtual Machines"
        )

        cluster_group_entity = Entity(cluster_group=cluster_group)

        if not quiet:
            print("\nIngesting ClusterGroup...")
        cluster_group_response = diode_client.ingest(entities=[cluster_group_entity])
        if cluster_group_response.errors:
            print(f"Errors during ClusterGroup ingestion: {cluster_group_response.errors}")
        else:
            print("Successfully ingested ClusterGroup")

        # Step 3: Create and ingest Clusters for each region
        regions = set(vm['location'] for vm in vm_data)
        region_clusters = {}

        for region in regions:
            cluster = Cluster(
                name=f"Azure-{region}",
                type=cluster_type,  # Use the ClusterType object directly
                group=cluster_group,  # Use the ClusterGroup object directly
                description=f"Azure VMs in {region} region",
                tags=["Azure"]
            )

            region_clusters[region] = cluster
            cluster_entity = Entity(cluster=cluster)

            if not quiet:
                print(f"\nIngesting Cluster for region {region}...")
            cluster_response = diode_client.ingest(entities=[cluster_entity])
            if cluster_response.errors:
                print(f"Errors during Cluster ingestion for region {region}: {cluster_response.errors}")
            else:
                print(f"Successfully ingested Cluster for region {region}")

        # Step 4: Create and ingest VMs and their disks
        for vm in vm_data:
            # Map Azure status to NetBox status
            vm_status = map_azure_status_to_netbox(vm['status'])

            # Convert float values to integers
            vcpus = int(vm['vcpus']) if vm['vcpus'] is not None else None
            memory = int(vm['memory_mb']) if vm['memory_mb'] is not None else None

            # Get the cluster for this VM's region
            vm_cluster = region_clusters.get(vm['location'])

            # Create VM entity with explicit cluster reference
            vm_entity = VirtualMachine(
                name=vm['name'],
                status=vm_status,
                cluster=vm_cluster,  # Use the Cluster object directly
                vcpus=vcpus,
                memory=memory,
                comments=f"Azure VM ID: {vm['id']}",
                tags=[
                    "Azure",
                    f"rg-{vm['resource_group']}",
                    f"size-{vm['vm_size']}"
                ]
            )

            if debug:
                print(f" Using cluster: {vm_cluster.name}")
                print(f" Cluster type: {vm_cluster.type.name}")
                print(f" Cluster group: {vm_cluster.group.name}")

            # Create a list to hold all entities for this VM (VM + disks)
            vm_entities = [Entity(virtual_machine=vm_entity)]

            # Step 5: Create and ingest VirtualDisk entities for each disk
            for disk in vm['disks']:
                # Skip disks with no size information
                if disk['size_gb'] is None:
                    if not quiet:
                        print(f"  Skipping disk {disk['name']} - no size information available")
                    continue

                # Create VirtualDisk entity
                disk_entity = VirtualDisk(
                    name=disk['name'],
                    virtual_machine=vm_entity,  # Associate with the VM
                    size=int(disk['size_gb']) * 1024,  # Size in MB
                    tags=["Azure"]
                )

                if not quiet:
                    print(f"  Adding disk: {disk['name']} ({disk['size_gb']} GB)")
                vm_entities.append(Entity(virtual_disk=disk_entity))

            # Step 6: Create and ingest VMInterface entities for each network interface
            nic_count = 0
            for nic in vm['network_interfaces']:
                nic_count += 1

                # Create a name for the interface if not available
                interface_name = nic['name'] if nic['name'] else f"eth{nic_count}"

                # Determine if this is the primary interface
                is_primary = nic['primary']

                # Create VMInterface entity
                interface_entity = VMInterface(
                    name=interface_name,
                    virtual_machine=vm_entity,  # Associate with the VM
                    enabled=True,
                    description=f"Azure NIC: {nic['id']}",
                    tags=["Azure"]
                )

                if not quiet:
                    print(f"  Adding interface: {interface_name}")
                vm_entities.append(Entity(vm_interface=interface_entity))

                # Step 7: Create and ingest IP addresses for this interface
                for ip_config in nic['ip_configurations']:
                    # Add private IP address with correct subnet prefix
                    if ip_config['private_ip']:
                        private_ip_with_prefix = get_ip_with_prefix(
                            ip_config['private_ip'], 
                            ip_config['subnet_prefix']
                        )

                        private_ip_entity = IPAddress(
                            address=private_ip_with_prefix,
                            status="active",
                            description=f"Private IP for {vm['name']} - {interface_name}",
                            assigned_object_vm_interface=interface_entity,  # Associate with the interface
                            tags=["Azure", "Private"]
                        )

                        if not quiet:
                            print(f"    Adding private IP: {private_ip_with_prefix}")
                        vm_entities.append(Entity(ip_address=private_ip_entity))

                    # Add public IP address if available (public IPs use /32)
                    if ip_config['public_ip']:
                        public_ip_with_prefix = f"{ip_config['public_ip']}/32"  # Public IPs are always /32

                        public_ip_entity = IPAddress(
                            address=public_ip_with_prefix,
                            status="active",
                            description=f"Public IP for {vm['name']} - {interface_name}",
                            assigned_object_vm_interface=interface_entity,  # Associate with the interface
                            tags=["Azure", "Public"]
                        )

                        if not quiet:
                            print(f"    Adding public IP: {public_ip_with_prefix}")
                        vm_entities.append(Entity(ip_address=public_ip_entity))

            # Ingest VM and all its components in a single request
            vm_response = diode_client.ingest(entities=vm_entities)
            if vm_response.errors:
                print(f"Errors during VM ingestion for {vm['name']}: {vm_response.errors}")
            else:
                print(f"Successfully ingested VM {vm['name']} with {len(vm_entities)-1} components")


    except Exception as e:
        print(f"\nError during ingestion: {e}")
if __name__ == "__main__":
    # Parse command-line arguments
    args = parse_args()

    # Get Azure VM data
    if not args.quiet:
        print("Retrieving Azure VM data...")
    vm_data = get_azure_vms()

    # Ingest data into NetBox
    if not args.quiet:
        print(f"Ingesting {len(vm_data)} VMs into NetBox...")
    ingest_to_netbox(vm_data, debug=args.debug, quiet=args.quiet)

    if not args.quiet:
        print("Ingestion complete!")