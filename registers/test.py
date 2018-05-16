import time
import threading
import logging
import socket
from random import randint
from urllib.request import urlopen
import re

def main():
    testSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    # testSocket.bind((''))
    testSocket.sendto(b'hahaha', ('127.0.0.1', 8808))
    print("done")


if __name__ == '__main__':
    main()
