from opcua import Client
from opcua import ua
import time
import sys


# Note: THIS CODE IS PURELY DEMONSTRATIVE AND YOU WILL NEED TO MAKE CHANGES IN ORDER TO APPLY IT FOR YOUR PROJECT
def read_codesys_variables():
    # Replace with your actual server URL
    server_url = "opc.tcp://127.0.0.1:4840"
    
    client = Client(server_url)

    # Connect to server
    client.connect()
    print(f"Connected to OPC UA Server at {server_url}")
    
    try:
        while True:
            
            # Access the nodes using their NodeId, namely the nodes which we will want to read from CODESYS
            # The names withing get_node() will depend on the configuration for each user, so change it at will.
            loadCin0 = client.get_node("ns=4;s=|var|CODESYS Control Win V3 x64.Application.PLC_PRG.MES_load_Cin0")
        
            # Read the values from the server
            print(f"MES_load_Cin0: {loadCin0.get_value()}")
            
            # Get the data types from the server
            data_type_loadCin0 = loadCin0.get_data_type_as_variant_type()
            
            # Print data types to help troubleshoot
            print(f"MES_load_Cin0 data type: {data_type_loadCin0}")

            for child in loadCin0.get_children():
                browse_name = child.get_browse_name()
                value = child.get_value()
                print(f"{browse_name.Name}: {value}")

            time.sleep(1)  # Adjust the sleep time as needed
                
            # Example of setting values (if needed) to CODESYS variables
            #Warehouse1.set_value(ua.Variant(Warehouse1.get_value() + 1, ua.VariantType.Int16)) # Example of setting a value

    except KeyboardInterrupt:
        # Close the server before exiting
        print("\nStopping client...")
        client.disconnect()
        print("Client stopped")
        sys.exit(0)
    
    except Exception as e:
        print(f"An error occurred: {e}")
        client.disconnect()
        sys.exit(1)

if __name__ == "__main__":
    values = read_codesys_variables()