import sys
import os
import logging

# Add current directory to path so we can import mail_transport
sys.path.append(os.getcwd())

from mail_transport import open_smtp_connection

# Setup logging to see output
logging.basicConfig(level=logging.INFO)

def test_connection():
    host = "smtp.gmail.com"
    port = 587
    print(f"Testing connection to {host}:{port}...")
    try:
        with open_smtp_connection(host, port, timeout=10) as server:
            print("Successfully connected to SMTP server!")
            print(f"Server response: {server.help()}")
    except Exception as e:
        print(f"Connection failed: {e}")

if __name__ == "__main__":
    test_connection()
