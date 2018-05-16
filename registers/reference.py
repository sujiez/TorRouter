import re
import sys
import socket
import threading
import ipaddress
import traceback
import datetime
import logging
from urllib.request import urlopen

'''
self.currentRequest -> {'sequenceNum':sequence number, 'timer':time out timer,
                        'retry':left retry time, 'message':message to send, 'commandNum':command number of this request,
                        <'portNum':port number>, <'serviceName':service name>, <'serviceData':service data>
                        'callback':callback function/None, 'callbackParam':callback parameter}

                    -> None, when the previous request is done and the new request have not been processed yet.


self.requestBuffer -> [{'sequenceNum':sequence number, 'timer':time out timer,
                        'retry':left retry time, 'message':message to send, 'commandNum':command number of this request,
                         <'portNum:'port number>, <'serviceName':service name>, <'serviceData':service data>
                         'callback':callback function/None, 'callbackParam':callback parameter},
                      ...]


self.addressPool -> {sequenceNum:{'portNum':port number, 'timer':time out timer, 'retry': left retry time,
                                  'serviceName':service name, 'serviceData':service data},
                ...}


self.portLookUp -> {portNum:sequenceNum, ...}
'''


class RegisterAgent:

    class agentTimer:
        def __init__(self):
            self.timer = None
            self.timerState = 0
            self.timerLock = threading.Lock()
            pass

        def startTimer(self, timeUpAction, timeup, parameter):
            self.timerLock.acquire()
            if self.timerState == 1:
                self.timerLock.release()
                return
            self.timerState = 1
            self.timer = threading.Timer(timeup, timeUpAction, [parameter])
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


    def __init__(self, port, serverIp, serverPort, checkIPUrl="http://checkip.dyndns.org", givenIP=None):
        '''

        :param port: the port to be bind with current agent (and port + 1 will be taken)
        :param serverIp: the IP of registeration server
        :param serverPort: the port of registeration server
        :param checkIPUrl: the URL to fetch external IP of this machine, optional
        :param givenIP: external IP of current machine, optional
        '''
        logging.basicConfig(level=logging.DEBUG, format='%(message)s')

        self.magic = 0xc461  # the magic number
        self.serverIp = self.resolveIp(serverIp) # get the IP address of the register server
        self.serverPort = serverPort
        if not givenIP:   # get the external IP of this machine
            self.localIP = self.getIP(checkIPUrl)  # fetch the external IP
        else:
            self.localIP = givenIP
            pass
        if not self.localIP:
            logging.error("The external IP of current machine cannot be acquired from given website!")
            logging.error("Please provide another valid URL to get IP in the parameter!")
            return

        header = self.giveDate()
        logging.info(header + " regServerIP = " + self.serverIp)
        logging.info(header + " thisHostIP = " + self.localIP)

        self.sequenceNum = 0  # current sequence number
        self.port = port  # the port of this agent
        self.doneState = 0  # if the agent quit
        self.retry = 5  # the retry times of resending massage
        self.timeUpTime = 3
        self.commandType = ['', 'REGISTER', '', 'FETCH', '', 'UNREGISTER', 'PROBE', '']

        # the request that is currently being processed, and handle request control flow
        self.currentRequest = None
        self.requestBuffer = [] # tuple of new requests
        self.addressPool = {} # ip, port -> tuple   information of address that already registered
        self.portLookUp = {}

        # the lock to lock 1. request buffer 2. sequence number 3. current request 4. addressPool 5.port look up
        self.requestBufferLock = threading.Lock()

        self.doneStateLock = threading.Lock() # lock the done state
        # for locking the positive socket, and also in charge
        self.positiveSocketLock = threading.Lock()

        self.nextRequestCV = threading.Condition(self.doneStateLock) # condition on if the previous request is finished

        self.positiveSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # the socket to send request and receive response
        self.passiveSocket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)  # the socket to response server probe

        self.requestSender = threading.Thread(target=self.processRequest)  # a thread that actually send request
        self.positiveProcessor = threading.Thread(target=self.listenPositively)
        self.passiveProcessor = threading.Thread(target=self.listenPassively)

        self.positiveSocket.bind(('0.0.0.0', port))
        self.passiveSocket.bind(('0.0.0.0', port + 1))

        self.requestSender.start()
        self.positiveProcessor.start()
        self.passiveProcessor.start()
        pass


    def listenPositively(self):
        while True:
            data, addr = self.positiveSocket.recvfrom(4096)
            if addr[0] != '127.0.0.1' or addr[1] != self.port + 1 or data != b'bye':
                self.positiveHandler(data)
            else:
                break
                pass
        pass


    def listenPassively(self):
        while True:
            data, addr = self.passiveSocket.recvfrom(4096)
            if addr[0] != '127.0.0.1' or addr[1] != self.port or data != b'bye':
                self.passiveHandler(addr, data)
            else:
                break
                pass
        pass


    def closeAgent(self):
        '''
        For terminating the agent system, need to be called when do not want
        to use this agent anymore in current program. Otherwise, the client
        program maybe cannot quit successfully.

        :return:
        '''
        self.nextRequestCV.acquire()  # to let the request processor quit
        self.doneState = 1
        self.nextRequestCV.notify()
        self.nextRequestCV.release()
        self.requestBufferLock.acquire()  # to stop all the timer
        if self.currentRequest:
            self.currentRequest['timer'].cancelTimer()
            pass
        for key in self.addressPool.keys():
            item = self.addressPool[key]
            item['timer'].cancelTimer()
            pass
        self.requestBufferLock.release()
        self.requestSender.join()

        self.positiveSocket.sendto('bye'.encode(), ('0.0.0.0', self.port + 1))
        self.passiveSocket.sendto('bye'.encode(), ('0.0.0.0', self.port))

        self.positiveSocket.close()
        self.passiveSocket.close()
        pass


    def giveDate(self):
        currentTime = datetime.datetime.now()
        result = '[' + str(currentTime.year) + "-" + str(currentTime.month) + "-" + str(currentTime.day) + " "
        result += str(currentTime.hour) + ":" + str(currentTime.minute) + ":" + str(currentTime.second) + "]"
        return result


    def printRegisterationInfo(self, ipNum, portNum, lifeTime):
        header = self.giveDate()
        logging.info(header + " Register " + ipNum + ":" +
              str(portNum) + " successful: lifetime = " +
              str(lifeTime))
        pass


    def printFetchInformation(self, information):
        header = self.giveDate()
        for index, info in enumerate(information):
            other = " [" + str(index) + "]  " +  info[0] + "  " + str(info[1]) + "  " + \
                    str(info[2]) + " ("  + hex(info[2]) + ")"

            logging.info(header + other)
            pass
        pass


    def passiveHandler(self, ip_port, data):
        '''
        In charge of receiving and responsing server probe message.

        :param ip_port: the IP and port of the sender
        :param data: the data received
        '''
        '''
        TODO:
            It is easy, when receive probe information, just generate ACK message, and send it.
            Just need to send it once.
            Remember to lock the corresponding lock (lock for socket)
        '''
        if len(data) < 4:
            if data.decode() == 'bye':
                return
            logging.error("Service host asks an incomplete request")
            return

        magic, sequenceNum, request, siders = self.extractRequest(data)
        if magic != self.magic or request != 6:
            logging.error("Received incorrect request from service host")
            return
        responseMessage = self.generateRequest(7, None, None, None, sequenceNum)
        self.passiveSocket.sendto(responseMessage, ip_port)
        header = self.giveDate()
        logging.info(header + " Probed be the server! With sequence Number " + str(sequenceNum))
        pass


    def extractRequest(self, data):
        result = []
        result.append(int.from_bytes(data[:2], "big"))  # magic number
        result.append(data[2])  # sequence number
        result.append(data[3])  # command number
        data = data[4:]
        if result[2] == 2:
            result.append(int.from_bytes(data, 'big'))  # life time
        elif result[2] == 4:
            numberOfEntry = int.from_bytes(data[:1], 'big')
            data = data[1:]
            friends = []
            for i in range(numberOfEntry):
                friendData = data[:10]
                data = data[10:]
                friendAddr = str(ipaddress.ip_address(int.from_bytes(friendData[:4], 'big')))
                friendData = friendData[4:]
                friendPort = int.from_bytes(friendData[:2], 'big')
                friendData = friendData[2:]
                friendServiceData = int.from_bytes(friendData, 'big')
                friends.append((friendAddr, friendPort, friendServiceData))
                pass
            result.append(friends)
        else:
            result.append(None)
            pass
        return result # [magicNumber, sequenceNumber, commandNumber, <data>]


    def registerTimeUp(self, pointer):
        '''
        The time up handler function for every registerd Ip and port

        :param handler: the timer handler
        :return:
        '''
        self.requestBufferLock.acquire()
        registrationInfo = self.addressPool[pointer]
        retryTime = registrationInfo['retry']
        portNum = registrationInfo['portNum']
        registrationInfo['timer'].timerState = 0
        if retryTime == 0:
            del self.portLookUp[portNum]
            del self.addressPool[pointer]
            self.requestBufferLock.release()

            header = self.giveDate()
            logging.info(header + " try to re-register port: " + str(portNum) + " but the server does not reponse! ")
            return

        if retryTime == self.retry:
            oldSequenceNum = self.sequenceNum
            registerMessage = self.generateRequest(1, portNum, registrationInfo['serviceData'],
                                                   registrationInfo['serviceName'], oldSequenceNum)
            registrationInfo['retry'] -= 1
            self.sequenceNum += 1
            self.sequenceNum %= (2 ** 8)
            del self.addressPool[pointer]
            self.addressPool[oldSequenceNum] = registrationInfo
            self.portLookUp[portNum] = oldSequenceNum

            registrationInfo['timer'].startTimer(self.registerTimeUp, self.timeUpTime, oldSequenceNum)
            self.requestBufferLock.release()
        else:
            registerMessage = self.generateRequest(1, portNum, registrationInfo['serviceData'],
                                                   registrationInfo['serviceName'], pointer)
            registrationInfo['retry'] -= 1
            registrationInfo['timer'].startTimer(self.registerTimeUp, self.timeUpTime, pointer)
            self.requestBufferLock.release()

        self.positiveSocketLock.acquire()
        self.positiveSocket.sendto(registerMessage, (self.serverIp, self.serverPort))
        self.positiveSocketLock.release()

        header = self.giveDate()
        logging.info(header + " try to re-register port: " + str(portNum) + "  " + str(self.retry + 1 - retryTime) + " times!")
        pass


    def responseTimeUp(self, handler):
        '''
        The time up handler function for every message time out

        :param handler: the timer handler
        :return:
        '''
        '''
        TODO:
            0. check if the current request is None
            1. get the current request
            2. check the retry time
                (1). signal the processor, when there is no retry time left
                (2). minus the retry time by one  -1s
            3. maybe need to resend message to server, and print retry message
            x. remember to lock corresponding lock
        '''
        self.requestBufferLock.acquire()
        if not self.currentRequest:
            self.requestBufferLock.release()
            return

        leftRetry = self.currentRequest['retry']
        commandNum = self.currentRequest['commandNum']
        self.currentRequest['timer'].timerState = 0
        if leftRetry == 0:
            header = self.giveDate()
            self.currentRequest = None
            self.nextRequestCV.acquire()
            self.nextRequestCV.notify()
            self.nextRequestCV.release()
            self.requestBufferLock.release()

            logging.info(header + " Sent " + str(self.retry) + " " + self.commandType[commandNum] + " messages but got no reply.")
            return

        self.currentRequest['retry'] -= 1
        self.currentRequest['timer'].startTimer(self.responseTimeUp, self.timeUpTime, None)
        self.requestBufferLock.release()

        self.positiveSocketLock.acquire()
        self.positiveSocket.sendto(self.currentRequest['message'], (self.serverIp, self.serverPort))
        self.positiveSocketLock.release()

        header = self.giveDate()
        logging.info(header + " Timed out waiting for reply to " + self.commandType[commandNum] + " message")
        pass


    def positiveHandler(self, data):
        '''
        In charge of receiving and responsing  server response message
        '''
        '''
        TODO:
            0. Check if current request is already None (Also check this in the timer up function)
            1. cancel corresponding timer
            2. set current request
            3. signal on conditional variable
            4. Check sequence number!!!
            5. handle all sorts of information
            x. Now here, we can meet two cases:
                    (1). Can be the response of current request
                    (2). Can also be the response of re-register request
        '''
        if len(data) < 4:
            logging.error("Service message too short!")
            return
        # get the sequence number and command from response packet
        magicNumber, sequenceNum, commandNum, siders = self.extractRequest(data)
        if magicNumber != self.magic:
            return

        self.requestBufferLock.acquire()
        if sequenceNum in self.addressPool and commandNum == 2:
            re_registerInfo = self.addressPool[sequenceNum]  # get the registered information
            re_registerInfo['timer'].cancelTimer()  # cancel the timer of current registration
            re_registerInfo['retry'] = self.retry  # reset retry
            re_registerInfo['timer'].startTimer(self.registerTimeUp, siders * 2 / 3.0, sequenceNum)  # restart the timer
            # re_registerInfo['timer'].startTimer(self.registerTimeUp, 5, sequenceNum)
            self.requestBufferLock.release()

            self.printRegisterationInfo(self.localIP, re_registerInfo['portNum'], siders)
            pass
        elif self.currentRequest and sequenceNum == self.currentRequest['sequenceNum']:
            if (self.currentRequest['commandNum'] == 1 and commandNum != 2) or \
               (self.currentRequest['commandNum'] == 3 and commandNum != 4) or \
               ((self.currentRequest['commandNum'] == 5 or
                self.currentRequest['commandNum'] == 6) and commandNum != 7):
                self.requestBufferLock.release()
                return

            self.currentRequest['timer'].cancelTimer()
            current = self.currentRequest
            self.currentRequest = None

            if commandNum == 2:
                new_timer = self.agentTimer()  # give a new timer for current registration
                new_registerIp = {'portNum':current['portNum'], 'timer':new_timer, 'retry':self.retry,
                                  'serviceName':current['serviceName'], 'serviceData':current['serviceData']}

                self.addressPool[sequenceNum] = new_registerIp  # add to the pool
                self.portLookUp[current['portNum']] = sequenceNum  # add to the look up

                new_timer.startTimer(self.registerTimeUp, siders * 2 / 3.0, sequenceNum)  # start timer
                # new_timer.startTimer(self.registerTimeUp, 5, sequenceNum)
            elif current['commandNum'] == 5:
                pointer = self.portLookUp[current['portNum']]
                self.addressPool[pointer]['timer'].cancelTimer()
                del self.portLookUp[current['portNum']]
                del self.addressPool[pointer]
                pass

            self.nextRequestCV.acquire()
            self.nextRequestCV.notify()
            self.nextRequestCV.release()
            self.requestBufferLock.release()

            if commandNum == 4:
                logging.info(str(len(siders)))
                self.printFetchInformation(siders)
            elif commandNum == 2:
                self.printRegisterationInfo(self.localIP, current['portNum'], siders)
            else:
                header = self.giveDate()
                logging.info(header + " Success with " + self.commandType[current['commandNum']])
                pass

            if current['callback']:
                return current['callback'](*current['callbackParam'])
        else:
            self.requestBufferLock.release()
            logging.error("May receive message that is not corresponding or current request is already time up! ")
            return


    def processRequest(self):
        '''
        For the thread to send requests.
        '''
        '''
        TODO:
            1. A while loop tp check if the done state is set, quit of it is.
            2. Immediately after entering the while loop, wait on the conditional variable
            3. When waiting, need to give the lock which you own
            4. REMEMBER TO CHECK if 'currentRequest' is None AND if the requestBuffer is empty !!!!!
            5. The line above is IMPORTANT!!!!!!!!!!!
            6. send request and set time up function, start timer if need
            7. remove the first request from the buffer
            8. lock the corresponding lock if need
        '''
        self.nextRequestCV.acquire()
        while True:
            self.nextRequestCV.wait()
            if self.doneState == 1:
                self.nextRequestCV.release()
                break

            self.requestBufferLock.acquire()
            if self.currentRequest or len(self.requestBuffer) == 0:
                self.requestBufferLock.release()
                continue

            self.currentRequest = self.requestBuffer.pop(0)
            logging.info("processing request " + str(self.currentRequest['commandNum']))
            requestMessage = self.currentRequest['message']
            requestTimer = self.currentRequest['timer']
            # put here because want to use the buffer lock to control the start of cancellation of timer
            requestTimer.startTimer(self.responseTimeUp, self.timeUpTime, None)

            self.requestBufferLock.release()
            # another set of operation
            self.positiveSocketLock.acquire()
            self.positiveSocket.sendto(requestMessage, (self.serverIp, self.serverPort))
            self.positiveSocketLock.release()
            logging.info("done")
        pass


    def getRequest(self, commandNum, portNum=None, serviceData=-1, serviceName="", callback=None, callbackParam=[]):
        '''
        External function that buffer requests need to be sent.

        :param commandNum: request command number
        :param portNum: port number to be registered, default to 'None'
        :param serviceData: service data, 32bit unsigned, default to 0
        :param serviceName: service name to be registered, default to empty string
        '''
        errorFlag = False
        errorMessage = []
        if (commandNum == 1 or commandNum == 3) and len(serviceName) > 255:
            errorFlag = True
            errorMessage.append("The service name given is too large! So service is not registered!")
            errorMessage.append("Please give string that is below 255 in length!")
        if (commandNum == 1 or commandNum == 5) and not portNum:
            errorFlag = True
            errorMessage.append(errorMessage.append("Port must be provided when want to register!"))
            errorMessage.append("Please give a ")
        if commandNum == 1 and serviceData > 4294967295:
            errorFlag = True
            errorMessage.append("The service data given is too large! So service is not registered!")
            errorMessage.append("Please give number that is smaller than 4294967295!")
        if commandNum != 1 and commandNum != 3 and commandNum != 5 and commandNum != 6:
            errorFlag = True
            errorMessage.append("Invide command number: " + str(commandNum))
            errorMessage.append("1 -> register; 3 -> fetch data; 5 -> unregister; 6 -> probe")
            pass
        if commandNum == 5:
            if portNum not in self.portLookUp:
                errorFlag = True
                errorMessage.append("Current port is not registered!")
            pass
        if errorFlag:
            for line in traceback.format_stack():
                logging.error(line.strip())
                pass
            for warn in errorMessage:
                logging.error(warn)
                pass
            return

        message = self.generateRequest(commandNum, portNum, serviceData, serviceName, self.sequenceNum)
        self.requestBufferLock.acquire()
        self.requestBuffer.append(self.generateRequestInfo(self.sequenceNum, message, commandNum, portNum, serviceName,
                                                           serviceData, callback, callbackParam))
        self.sequenceNum += 1
        self.sequenceNum %= (2 ** 8)
        self.nextRequestCV.acquire()
        self.nextRequestCV.notify()
        self.nextRequestCV.release()
        self.requestBufferLock.release()
        pass

    def generateRequestInfo(self, sequenceNum, message, commandNum, portNum, serviceName,
                            serviceData, callback, callbackParam):
        result = {'sequenceNum':sequenceNum, 'timer':self.agentTimer(),
                'retry':self.retry, 'message':message, 'commandNum':commandNum,
                  'callback':callback, 'callbackParam':callbackParam}
        if portNum:
            result['portNum'] = portNum
            pass
        if serviceName:
            result['serviceName'] = serviceName
            pass
        if serviceData != -1:
            result['serviceData'] = serviceData
        return result


    def generateRequest(self, commandNum, portNum, serviceData, serviceName, sequenceNum):
        '''
        For generate request message.

        :param commandNum: Command number
        :param portNum: port number
        :param serviceData: service data
        :param serviceName: service name
        :return: the message generated, in bytes
        '''
        result = self.magic.to_bytes(2, 'big')
        result += sequenceNum.to_bytes(1, 'big')
        result += commandNum.to_bytes(1, 'big')
        if commandNum == 1:
            result += socket.inet_aton(self.localIP) # ip number
            result += portNum.to_bytes(2, 'big')  # port number
            result += serviceData.to_bytes(4, 'big')  # service data
            result += len(serviceName).to_bytes(1, 'big')  # length of service name
            result += serviceName.encode()  # service name
        elif commandNum == 3:
            result += len(serviceName).to_bytes(1, 'big')  # service name length
            result += serviceName.encode()  # service name
        elif commandNum == 5:
            result += socket.inet_aton(self.localIP)  # ip number
            result += portNum.to_bytes(2, 'big')  # port number
        return result


    def getIP(self, checkIPUrl):
        '''
        Fetch external Ip of current machine, from the given URL.

        :param checkIPUrl: the given URL
        :return: the IPv4 address that fetched, in string
        '''
        message = urlopen(checkIPUrl).read().decode()
        results = re.findall('[0-9]{1,3}.[0-9]{1,3}.[0-9]{1,3}.[0-9]{1,3}', message)
        if len(results) == 0:
            return None
        return results[0]

    def resolveIp(self, serverIp):
        '''
        Get the Ipv4 for registration server

        :param serverIp: URL of registration server
        :return:
        '''
        return socket.gethostbyname_ex(serverIp)[-1][0]