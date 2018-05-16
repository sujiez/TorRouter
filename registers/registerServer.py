import threading
import socket
import ipaddress
import logging
import time

'''
RegisterServer.registerPool -> {(serviceIp, servicePort) -> { 'serviceIp':ipv4 registered (int), 'servicePort':port registered(int),
                  'serviceData':data registered (int), 'serviceName':name registered,
                  'timeup':time out time, 'timer':timer out timer, 'agentPort':port of agent},
                  ...}
'''
logging.basicConfig(level=logging.DEBUG, format='%(message)s')

class RegisterServer:

    class WrappedTimer:
        def __init__(self):
            self.timer = None
            self.timerState = 0
            self.timerLock = threading.Lock()
            pass

        def startTimer(self, timeUpAction, timeup, parameter):
            if type(parameter) is not list:
                raise ValueError("parameter given need to be list")
            self.timerLock.acquire()
            if self.timerState == 1:
                self.timerLock.release()
                return
            self.timerState = 1
            self.timer = threading.Timer(timeup, timeUpAction, parameter)
            self.timer.start()
            self.timerLock.release()
            pass

        def cancelTimer(self):
            self.timerLock.acquire()
            if self.timerState == 0:
                self.timerLock.release()
                return
            self.timerState = 0
            self.timer.cancel()
            self.timerLock.release()
            pass


    def __init__(self, serverIp, serverPort, lifetime):
        """

        :param serverIp:
        :param serverPort:
        :param lifetime
        """

        self.serverIp = serverIp
        self.serverPort = serverPort
        self.lifetime = lifetime
        self.checkSum = 0xc461

        self.poolLock = threading.Lock()
        self.registerPool = {}

        self.socketLock = threading.Lock()
        self.serverSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.serverSocket.bind((self.serverIp, self.serverPort))

        self.receptient = threading.Thread(target=self.startReceive)

        self.receptient.start()
        pass


    def terminate(self):
        self.socketLock.acquire()
        self.serverSocket.sendto('bye'.encode(), ('0.0.0.0', self.serverPort))
        self.socketLock.release()

        self.poolLock.acquire()
        for key, value in self.registerPool.items():
            value['timer'].cancelTimer()
        self.poolLock.release()

        self.serverSocket.close()
        print("Server shuts down \n")
        pass


    def startReceive(self):
        while True:
            data, addr = self.serverSocket.recvfrom(4096)
            if addr[0] != '127.0.0.1' or addr[1] != self.serverPort:
                requestProcessor = threading.Thread(target=self.requestProcess, args=[data, addr], daemon=False)
                requestProcessor.start()
            else:
                break
            pass
        pass

    def extractRequest(self, data):
        checkSum = int.from_bytes(data[:2], 'big')
        seqNum = int.from_bytes(data[2:3], 'big')
        command = data[3]
        return (checkSum, seqNum, command)


    def requestProcess(self, data, addr):
        logging.info("enter here")
        checkSum, seqNum, command = self.extractRequest(data)

        if checkSum != self.checkSum or seqNum is None or command is None:
            pass
        # print("overhere")
        # print("command is " + str(command))
        # print("type is " + str(type(command)))
        if command == 6:
            logging.info("Probed by agent addr: " + str(addr))

            response = self.constructResponse(seqNum, 7)
            self.socketLock.acquire()
            self.serverSocket.sendto(response, addr)
            self.socketLock.release()

            logging.info("Probe Success!")
            pass

        elif command == 5:
            curIp = str(ipaddress.ip_address(int.from_bytes(data[4:8], 'big')))
            curPort = int.from_bytes(data[8:10], 'big')

            logging.info("Unregister From agent addr: " + str(addr))
            logging.info("Ip: " + str(curIp) + " port: " + str(curPort))


            self.poolLock.acquire()

            self.registerPool[(curIp, curPort)]['timer'].cancelTimer()
            del self.registerPool[(curIp, curPort)]

            self.poolLock.release()

            response = self.constructResponse(seqNum, 7)
            self.socketLock.acquire()
            self.serverSocket.sendto(response, addr)
            self.socketLock.release()

            logging.info("Unregister success! ")
            pass

        elif command == 3:
            nameLen = data[4]
            name = str(data[5:nameLen + 5].decode('utf-8'))


            logging.info("Fetch from agent addr: " + str(addr))
            logging.info("Service name len: " + str(nameLen))
            logging.info("Service name: " + str(name))

            resultList = []
            self.poolLock.acquire()
            for key, value in self.registerPool.items():
                if not name or \
                        (len(name) <= len(value['serviceName']) and value['serviceName'][:len(name)]):
                    resultList.append([value['serviceIp'], value['servicePort'], value['serviceData']])
            self.poolLock.release()

            logging.info("find out: " + str(resultList))

            response = self.constructResponse(seqNum, 4)
            trueLen = min((65507 - 5) / 10, len(resultList))
            response += trueLen.to_bytes(1, 'big')
            for index in range(trueLen):
                response += socket.inet_aton(resultList[index][0])
                response += resultList[index][1].to_bytes(2, 'big')
                response += resultList[index][2].to_bytes(4, 'big')
            self.socketLock.acquire()
            self.serverSocket.sendto(response, addr)
            self.socketLock.release()

            logging.info("fetch success! ")
            pass

        elif command == 1:
            curIp = str(ipaddress.ip_address(int.from_bytes(data[4:8], 'big')))
            curPort = int.from_bytes(data[8:10], 'big')
            curData = int.from_bytes(data[10:14], 'big')
            nameLen = 15 + data[14]
            curName = data[15:nameLen].decode('utf-8')

            self.poolLock.acquire()
            if (curIp, curPort) not in self.registerPool:
                curTimer = RegisterServer.WrappedTimer()
                curTimer.startTimer(self.registerTimeUp, self.lifetime, [curIp, curPort])
                curEntry = {
                    'serviceIp': curIp,
                    'servicePort': curPort,
                    'serviceData': curData,
                    'serviceName': curName,
                    'timer': curTimer,
                    'lastTime': time.time()
                }

                logging.info("Register from agent: " + str(addr))
                logging.info("info: " + str(curEntry))

                self.registerPool[(curIp, curPort)] = curEntry
                self.poolLock.release()

            else:
                logging.info("Reregister from agent: " + str(addr))
                logging.info("ip and port: " + str((curIp, curPort)))

                current = self.registerPool[(curIp, curPort)]

                current['timer'].cancelTimer()
                current['timer'].startTimer(self.registerTimeUp, self.lifetime, [curIp, curPort])

                current['lastTime'] = time.time()
                self.poolLock.release()



            response = self.constructResponse(seqNum, 2)
            response += self.lifetime.to_bytes(2, 'big')
            self.socketLock.acquire()
            self.serverSocket.sendto(response, addr)
            self.socketLock.release()
            logging.info("Register success!")
            pass
        pass


    def constructResponse(self, sequenceNum, command):
        r = self.checkSum.to_bytes(2, 'big')
        r += sequenceNum.to_bytes(1, 'big')
        r += command.to_bytes(1, 'big')
        return r

    def registerTimeUp(self, ip, port):
        logging.info(str((ip, port)) + " time up!")
        self.poolLock.acquire()
        if (time.time() - self.registerPool[(ip, port)]['lastTime']) < self.lifetime or \
                        (ip, port) not in self.registerPool:
            self.poolLock.release()
            return

        del self.registerPool[(ip, port)]
        self.poolLock.release()
        logging.info("success")
        pass

def main():
    server = RegisterServer('0.0.0.0', 9909, 240)
    server.startReceive()
    while True:
        try:
            line = input()
            if line == 'q':
                break
            elif line == 'r':
                server.poolLock.acquire()
                logging.info(str(server.registerPool))
                server.poolLock.release()
        except EOFError:
            break
        pass

    server.terminate()
    pass



if __name__ == '__main__':
    main()
