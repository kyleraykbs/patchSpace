#!/usr/bin/env python3
"""Test script to verify pw-dump output and parsing."""

import subprocess
import json
import sys


def test_pwdump():
    print("Testing pw-dump...")
    try:
        # Test basic pw-dump
        result = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=2)
        if result.returncode != 0:
            print(f"pw-dump failed: {result.stderr}")
            return False

        print(f"pw-dump produced {len(result.stdout)} bytes of output")

        # Try to parse it
        try:
            data = json.loads(result.stdout)
            if isinstance(data, list):
                print(f"Successfully parsed {len(data)} objects")
                # Count types
                types = {}
                for obj in data:
                    obj_type = obj.get("type", "unknown")
                    types[obj_type] = types.get(obj_type, 0) + 1

                print("\nObject types found:")
                for type_name, count in types.items():
                    print(f"  {type_name}: {count}")

                return True
            else:
                print(f"Unexpected data type: {type(data)}")
                return False
        except json.JSONDecodeError as e:
            print(f"JSON parse error: {e}")
            # Show first 200 chars
            print(f"First 200 chars: {result.stdout[:200]}")
            return False

    except subprocess.TimeoutExpired:
        print("pw-dump timed out")
        return False
    except Exception as e:
        print(f"Error: {e}")
        return False


def test_pwdump_monitor():
    print("\nTesting pw-dump -m (monitor mode)...")
    try:
        proc = subprocess.Popen(
            ["pw-dump", "-m"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

        # Read first chunk
        import time

        time.sleep(1)

        # Try to read some data
        import select

        ready, _, _ = select.select([proc.stdout], [], [], 2)
        if ready:
            chunk = proc.stdout.read(4096)
            print(f"Got {len(chunk)} bytes from monitor mode")
            print(f"First 200 chars: {chunk[:200]}")
        else:
            print("No data from monitor mode after 2 seconds")

        proc.terminate()
        proc.wait(timeout=2)

    except Exception as e:
        print(f"Error testing monitor mode: {e}")


if __name__ == "__main__":
    test_pwdump()
    test_pwdump_monitor()
