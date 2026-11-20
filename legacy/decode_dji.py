import struct
import csv
import sys
import os

def read_varint(data, offset):
    result = 0
    shift = 0
    while offset < len(data):
        b = data[offset]
        result |= (b & 0x7f) << shift
        offset += 1
        if not (b & 0x80):
            return result, offset
        shift += 7
    return 0, offset

def extract_quaternions_from_payload(payload):
    quats = []
    idx = 0
    while idx < len(payload):
        if idx + 10 > len(payload): break
        key, idx = read_varint(payload, idx)
        if key == 0: break
        field_num = key >> 3
        wire_type = key & 0x07
        
        if wire_type == 0:
            _, idx = read_varint(payload, idx)
        elif wire_type == 1:
            idx += 8
        elif wire_type == 2:
            length, idx = read_varint(payload, idx)
            # DJI nested IMU Quaternion is Field 3, Len 20
            if field_num == 3 and length == 20:
                sub = payload[idx:idx+length]
                # Validate the nested floats (fields 1, 2, 3, 4 with wiretype 5)
                if sub[0] == 0x0d and sub[5] == 0x15 and sub[10] == 0x1d and sub[15] == 0x25:
                    f1 = struct.unpack('<f', sub[1:5])[0]
                    f2 = struct.unpack('<f', sub[6:10])[0]
                    f3 = struct.unpack('<f', sub[11:15])[0]
                    f4 = struct.unpack('<f', sub[16:20])[0]
                    quats.append((f1, f2, f3, f4))
            elif length > 0 and idx + length <= len(payload):
                nested_quats = extract_quaternions_from_payload(payload[idx:idx+length])
                quats.extend(nested_quats)
            idx += length
        elif wire_type == 5:
            idx += 4
        else:
            break
    return quats

def main(bin_file, sizes_file, output_csv):
    print(f"Reading packet sizes from {sizes_file}...")
    with open(sizes_file, "rb") as f:
        sizes_data = f.read()
    
    packet_sizes = [struct.unpack('>I', sizes_data[i:i+4])[0] for i in range(0, len(sizes_data), 4)]
    
    print(f"Extracting Quaternions from {bin_file}...")
    all_quats = []
    offset = 0
    with open(bin_file, "rb") as f:
        data = f.read()
        
    for size in packet_sizes:
        if offset + size > len(data): break
        packet_data = data[offset:offset+size]
        all_quats.extend(extract_quaternions_from_payload(packet_data))
        offset += size
        
    print(f"Success! Extracted {len(all_quats)} frames of motion data.")
    
    print(f"Writing to {output_csv}...")
    with open(output_csv, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['timestamp', 'qw', 'qx', 'qy', 'qz'])
        # 800Hz sample rate = 0.00125 seconds per sample
        for i, (w, x, y, z) in enumerate(all_quats):
            t = i * 0.00125
            writer.writerow([f"{t:.6f}", w, x, y, z])
            
    print("Done! Ready for Gyroflow.")

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python decode_dji.py <djmd1.bin> <djmd1.bin.sizes>")
        sys.exit(1)
        
    bin_file = sys.argv[1]
    sizes_file = sys.argv[2]
    output_csv = bin_file.replace(".bin", "_gyroflow.csv")
    
    if os.path.exists(bin_file) and os.path.exists(sizes_file):
        main(bin_file, sizes_file, output_csv)
    else:
        print("Error: Could not find the specified files.")
