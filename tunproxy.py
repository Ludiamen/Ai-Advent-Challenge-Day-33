"""ProxyCommand для ssh через заданный сетевой интерфейс (День 33, DEPLOY.md).

Сеть офиса закрывает нестандартный порт SSH VPS, а VPN (tun2) его пропускает. Привязка сокета
к интерфейсу (SO_BINDTODEVICE) ведёт одно соединение через VPN, не меняя маршрутов системы и
без sudo. В ~/.ssh/config:  ProxyCommand python3 /путь/к/tunproxy.py %h %p tun2
"""

import os
import selectors
import socket
import sys


def main() -> int:
    хост, порт, интерфейс = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    с = socket.socket()
    с.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, интерфейс.encode())
    с.settimeout(20)
    с.connect((хост, порт))
    с.settimeout(None)
    отбор = selectors.DefaultSelector()
    отбор.register(0, selectors.EVENT_READ)
    отбор.register(с, selectors.EVENT_READ)
    while True:
        for ключ, _ in отбор.select():
            if ключ.fileobj == 0:
                данные = os.read(0, 65536)
                if not данные:
                    с.shutdown(socket.SHUT_WR)
                    отбор.unregister(0)
                    continue
                с.sendall(данные)
            else:
                данные = с.recv(65536)
                if not данные:
                    return 0
                os.write(1, данные)


if __name__ == "__main__":
    sys.exit(main())
