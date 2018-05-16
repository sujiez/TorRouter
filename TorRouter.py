#!/usr/bin/env python3
import re
import socket
import threading
import logging
import sys
import argparse
from random import randint

sys.path.append("./registers")
from registerAgent import RegisterAgent

logging.basicConfig(level=logging.DEBUG, format='%(message)s')


R_PORT = 8808

R_SERVER_IP = 'cse461.cs.washington.edu'
R_SERVER_PORT = 46101

T_CONNECT_PORT = 6666
# PROXY_PORT = 10000

class CircuitWrapper:
    '''
    Store the information of one entry in the circuit table
    '''

    def __init__(self, isEnd, nextHop, circuitNumber):
        self.isEnd = isEnd  # indicate whether this is the end of current circuit

        # the next hop from current point of current circuit, (agent#, 0/1, next circuit#)
        # could be None if current point is end of current circuit
        self.circuitNumber = circuitNumber

        # (agent#, 0/1, circuit#)
        self.nextHop = nextHop

        '''
        Wait on the outermost lock

        self.requestBuffer -> [{'requestNum':0 -> process relay ex/1 -> process relay begin/2 -> send relay begin,
                                'requestInfo': (ip, port, agentNum) / (ip, port, stream#) / (ip, port) 
                                'condition':cv/None,
                                'response':[(True/False)]/None,
                                'streamNum':current stream ID if 2 / None if other
                                            (we get request, first add this entry)}, ...]
        self.currentRequest -> previous head of self.requestBuffer
        '''
        # self.cvLock = dataLock # lock the
        self.requestLock = threading.Lock()
        self.requestCondition = threading.Condition(self.requestLock)

        self.responseLock = threading.Lock()
        self.responseCondition = threading.Condition(self.responseLock)
        # locked by requestLock
        self.terminate = False

        self.currentRequest = None
        self.requestBuffer = []
        self.requestResponse = None

        self.streamCount = 1
        pass

    def __str__(self):
        result = "This is circuit " + str(self.circuitNumber) + "\n"
        result += "The next hop is " + str(self.nextHop) + "\n"
        result += "The size of request buffer is " + str(len(self.requestBuffer)) + "\n"
        for term in self.requestBuffer:
            result += term['requestNum'] + ", "
            pass
        return result + "\n"


class SocketWrapper:
    '''
    Store information about connections
    '''

    '''
    self.circuitTable -> {circuitNum:CircuitWrapper, ...}

    self.requestBuffer -> [requestInfo]
        requestInfo(a dict): 'requestType': 1 (send create circuit) / 2 (send rel ex)
                             'condition': when the request is processed, call customer cv
                             'previousHop': (agentID, 0/1, circuit#)
                                          previous connected point, can be None is asked by startup process (only if 1)
                             'nextDest': (ip, port, agentID) (only if 2)
                             'response': []/None
                                None when given, if rel ex [state], if create circuit [state, circuitNum(this side)]
                             'circuitNum': current circuit number (valid only when 2),
                                            tell the processor on which circuit the client want to extend.
                            
    self.currentRequest -> an entry of requestBuffer
    self.requestResponse -> the matched response entry
    '''
    def __init__(self, routerSocket, connector, connectionKey):
        self.routerSocket = routerSocket # the socket that connect to other router

        self.connector = connector  # whether current router is the connector

        # if need acquire both lock, acquire the dataLocker first
        self.socketLock = threading.Lock()  # for lock the circuitTable
        self.dataLock = threading.Lock()  # for lock the socket

        self.circuitTable = {} # contain the router table

        self.connectionKey = connectionKey
        self.circuitCount = 1

        # if need to acquire both socketLocker and requestBufferLock, get socketLocker first
        self.requestBufferLock = threading.Lock()
        self.responseBufferLock = threading.Lock()
        self.requestBuffer = []
        self.currentRequest = None
        # boolean to indicate success or fail
        self.requestResponse = None
        # self.shutDown = False

        # wait on response of current request
        self.responseCondition = threading.Condition(self.responseBufferLock)
        # wait on the buffer
        self.requestCondition = threading.Condition(self.requestBufferLock)

        # locked by the request condition
        self.terminate = False
        pass

    def __str__(self):
        result = "This is connection with key " + str(self.connectionKey) + "\n"
        result += "I am connector " + str(self.connector) + "\n"
        result += "The ciruit table has elememnt " + str(len(self.circuitTable)) + "\n"
        for k, v in self.circuitTable.items():
            result += str(k) + " "
            if not v.isEnd:
                result += str(v.nextHop)
            result += ", "
            pass
        result += "\n"
        result += "The request buffer has element " + str(len(self.requestBuffer)) + "\n"
        for term in self.requestBuffer:
            result += term['requestType'] + ", "
            pass
        result += "\n"
        return result


class ProxyInterface:
    '''
    Stores information about one stream (Between proxy socket and routerSocket)
    '''
    def __init__(self, proxySocket, proxySocketLock):
        # self.routerSocket = routerSocket
        self.proxySocket = proxySocket

        self.proxySocketLock = proxySocketLock
        pass


class TorRouter:
    '''
    The tor router
    '''

    '''
    self.connectionInfo -> { (agent#, 0/1) -> SocketWrapper, ...}

                            agent# means the agent connected
                            0 means I am connector 
                            1 means I am connected (only used in self connection)

    self.interfaceInfo -> {((agent#, 1/0), circuit#, stream#) -> Interface}

                            ip, port specify the connection
                            circuit# specify the circuit
                            stream# specify the stream
    '''

    def __init__(self, registerAgent, connectPort, proxyPort, groupID, instanceID):
        if groupID < 1 or instanceID < 1 or groupID > 65536 or instanceID > 65536:
            raise ValueError("groupID or instanceID not OK!")

        self.groupID = groupID
        self.instanceID = instanceID
        self.agentId = (groupID << 16) + instanceID
        self.registerAgent = registerAgent  # the register agent for fetch and register

        self.connectPort = connectPort  # the port that for other router to connect
        self.proxyPort = proxyPort  # the port for proxy use (let broswer connect in)

        self.connectionInfo = {}  # store information and mechanism for connections
        self.interfaceInfo = {}  # stores information about stream (proxy <-> router interface)

        self.allDataLock = threading.Lock()  # lock both connectionInfo and interfaceInfo

        self.connectionSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.proxySocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

        self.connectionSocket.bind(('0.0.0.0', self.connectPort))
        self.proxySocket.bind(('0.0.0.0', self.proxyPort))

        self.headerConnection = None  # the header information (one of SocketWrapper)
        self.headCircuit = None
        self.cellLength = 512
        pass


    def runRouter(self):
        self.registerAgent.startRegister()
        threading.Thread(target=self.acceptRouterConnection).start()

        returnedState = self.registrationHelper(commandNum=1, portNum=self.connectPort, serviceData=self.agentId,
                                                serviceName="Tor61Router-" + str(self.groupID) + "-" + str(self.instanceID))
        if not returnedState[0]:
            raise ValueError("Cannot register myself\n")
        self.startUpCircuit()
        threading.Thread(target=self.acceptProxyConnection).start()
        pass


    def acceptProxyConnection(self):
        try:
            self.proxySocket.listen(5)
            while True:
                clientSock, clientAddr = self.proxySocket.accept()

                threading.Thread(target=self.processProxyRequest, args=[clientSock]).start()
                pass
        except socket.error as e:
            pass
        finally:
            self.proxySocket.close()
            pass
        pass


    def processProxyRequest(self, clientSock):
        try:
            clientSock.settimeout(5)
            endLine = [b'\r\n\r\n', b'\r\n\n', b'\n\n', b'\n\r\n']
            result = ""  # the whole information (convert to str from byte) get
            data = b''  # the data part of http request
            end = False  # if find separator
            while True:
                while True:
                    words = clientSock.recv(490)
                    for line in endLine:
                        if line in words:
                            end = True
                            parts = words.split(line)
                            result += parts[0].decode()
                            data += line.join(parts[1:])
                            break
                        pass
                    if not end:
                        result += words.decode()
                    else:
                        break
                    if len(words) != 490:
                        break
                    pass
                if end:
                    break
                if not result:
                    return
                pass

            header = result.split('\n')
            logInfo = "In processProxyRequest >>> received client information!\n"
            logInfo += "With header " + header[0]
            logging.info(logInfo)

            if "CONNECT" in header[0]:
                self.establishProxyConnection(clientSock, header, data)
            else:
                self.fetchProxyData(clientSock, header, data)
                pass
            pass
        except socket.error as e:
            pass
        finally:
            clientSock.close()
            pass
        pass


    def openProxyTunnel(self, socketInstance, circuitNum, streamNum, webSocket, webSocketLock):
        try:
            while True:
                quitFromRead = False
                while True:
                    serverMessage = webSocket.recv(490)
                    if len(serverMessage) == 0:
                        quitFromRead = True
                        break
                    messageCell = self.generateRequestMessage(commandNumber=3, circuitNumber=circuitNum,
                                                              streamId=streamNum, relayCmd=2, body=serverMessage)
                    socketInstance.socketLock.acquire()
                    socketInstance.routerSocket.sendall(messageCell)
                    if socketInstance.socketLock.locked():
                        socketInstance.socketLock.release()
                    if len(serverMessage) != 490:
                        break
                    pass
                if quitFromRead:
                    break
                pass
        except socket.error as e:
            logging.info("&&&&&& socket error in openProxyTunnel " + str(e))
        finally:
            if not webSocketLock.locked():
                webSocketLock.acquire()
            webSocket.close()
            if webSocketLock.locked():
                webSocketLock.release()

            if not socketInstance.socketLock.locked():
                socketInstance.socketLock.acquire()
                pass
            relayEndInfo = self.generateRequestMessage(commandNumber=3, circuitNumber=circuitNum,
                                                       streamId=streamNum, relayCmd=3, body=b'')
            socketInstance.routerSocket.sendall(relayEndInfo)
            if socketInstance.socketLock.locked():
                socketInstance.socketLock.release()

            self.allDataLock.acquire()
            del self.interfaceInfo[(socketInstance.connectionKey, circuitNum, streamNum)]
            self.allDataLock.release()
            pass
        pass


    def establishProxyConnection(self, clientSocket, header, data):
        hostIp, port, message = self.processProxyMessage(header, data, False)

        headCircuitInstance = self.headerConnection.circuitTable[self.headCircuit]
        relayBeginLock = threading.Lock()
        relayBeginCondition = threading.Condition(relayBeginLock)
        relayBeginRequest = {'requestNum':2, 'requestInfo':(hostIp, port), 'condition':relayBeginCondition,
                             'response':None, 'streamNum':None}
        relayBeginCondition.acquire()

        headCircuitInstance.requestCondition.acquire()
        headCircuitInstance.requestBuffer.append(relayBeginRequest)
        headCircuitInstance.requestCondition.notify()
        headCircuitInstance.requestCondition.release()

        relayBeginCondition.wait()

        if not relayBeginRequest['response'][0]:
            logging.info("In establishProxyConnection >>> proxy router connection established\n")
            relayBeginCondition.release()
            errorMessage = b"HTTP/1.1 502 Bad Gateway\r\n\r\n"
            clientSocket.sendall(errorMessage)
            return
        streamNum = relayBeginRequest['streamNum']
        relayBeginCondition.release()

        clientSocketLock = threading.Lock()
        self.allDataLock.acquire()
        self.interfaceInfo[(self.headerConnection.connectionKey, self.headCircuit, streamNum)] = ProxyInterface(clientSocket, clientSocketLock)
        self.allDataLock.release()

        okMessage = b"HTTP/1.1 200 OK\r\n\r\n"
        try:
            clientSocketLock.acquire()
            clientSocket.sendall(okMessage)  # send ok message
            if clientSocketLock.locked():
                clientSocketLock.release()
            while True:
                # clientMessage = b''
                quitFromRead = False
                while True:
                    clientMessage = clientSocket.recv(490)
                    # clientMessage += addOn
                    if len(clientMessage) == 0:
                        quitFromRead = True
                        break
                    messageCell = self.generateRequestMessage(commandNumber=3, circuitNumber=self.headCircuit,
                                                              streamId=streamNum, relayCmd=2, body=clientMessage)
                    self.headerConnection.socketLock.acquire()
                    self.headerConnection.routerSocket.sendall(messageCell)
                    if self.headerConnection.socketLock.locked():
                        self.headerConnection.socketLock.release()
                    if len(clientMessage) != 490:
                        break
                    pass
                if quitFromRead:
                    break
                pass
        except socket.error as e:
            logging.warning("&&&&&& socket error in establishProxyConnection " + str(e))
        finally:
            if not clientSocketLock.locked():
                clientSocketLock.acquire()
            clientSocket.close()
            if clientSocketLock.locked():
                clientSocketLock.release()

            if not self.headerConnection.socketLock.locked():
                self.headerConnection.socketLock.acquire()
                pass
            # 1. send relay end.  2. delete interface entry
            relayEndInfo = self.generateRequestMessage(commandNumber=3, circuitNumber=self.headCircuit,
                                                       streamId=streamNum, relayCmd=3, body=b'')
            self.headerConnection.routerSocket.sendall(relayEndInfo)
            if self.headerConnection.socketLock.locked():
                self.headerConnection.socketLock.release()

            self.allDataLock.acquire()
            del self.interfaceInfo[(self.headerConnection.connectionKey, self.headCircuit, streamNum)]
            self.allDataLock.release()
            pass
        pass


    def fetchProxyData(self, clientSocket, header, data):
        hostIp, port, message = self.processProxyMessage(header, data, False)
        headCircuitInstance = self.headerConnection.circuitTable[self.headCircuit]
        relayBeginLock = threading.Lock()
        relayBeginCondition = threading.Condition(relayBeginLock)
        relayBeginRequest = {'requestNum': 2, 'requestInfo': (hostIp, port), 'condition': relayBeginCondition,
                             'response': None, 'streamNum': None}
        relayBeginCondition.acquire()
        headCircuitInstance.requestCondition.acquire()
        headCircuitInstance.requestBuffer.append(relayBeginRequest)
        headCircuitInstance.requestCondition.notify()
        headCircuitInstance.requestCondition.release()
        relayBeginCondition.wait()

        if not relayBeginRequest['response'][0]:
            relayBeginCondition.release()
            logging.info("In fetchProxyData >>> proxy router connection established\n")
            clientSocket.close()
            return
        streamNum = relayBeginRequest['streamNum']
        relayBeginCondition.release()

        clientSocketLock = threading.Lock()
        self.allDataLock.acquire()
        self.interfaceInfo[(self.headerConnection.connectionKey, self.headCircuit, streamNum)] = ProxyInterface(clientSocket, clientSocketLock)
        self.allDataLock.release()

        try:
            self.headerConnection.socketLock.acquire()
            while message:
                chunk = message[:490]
                message = message[490:]
                messageCell = self.generateRequestMessage(commandNumber=3, circuitNumber=self.headCircuit,
                                                          streamId=streamNum, relayCmd=2, body=chunk)
                self.headerConnection.routerSocket.sendall(messageCell)
                pass
            if self.headerConnection.socketLock.locked():
                self.headerConnection.socketLock.release()

            while True:
                quitFromRead = False
                while True:
                    clientMessage = clientSocket.recv(490)
                    if len(clientMessage) == 0:
                        quitFromRead = True
                        break
                    messageCell = self.generateRequestMessage(commandNumber=3, circuitNumber=self.headCircuit,
                                                              streamId=streamNum, relayCmd=2, body=clientMessage)
                    self.headerConnection.socketLock.acquire()
                    self.headerConnection.routerSocket.sendall(messageCell)
                    if self.headerConnection.socketLock.locked():
                        self.headerConnection.socketLock.release()
                    if len(clientMessage) != 490:
                        break
                    pass
                if quitFromRead:
                    break
                pass
        except socket.error as e:
            logging.warning("&&&&&& socket error in fetchProxyData " + str(e))
        finally:
            if not clientSocketLock.locked():
                clientSocketLock.acquire()
            clientSocket.close()
            if clientSocketLock.locked():
                clientSocketLock.release()

            if not self.headerConnection.socketLock.locked():
                self.headerConnection.socketLock.acquire()
                pass
            relayEndInfo = self.generateRequestMessage(commandNumber=3, circuitNumber=self.headCircuit,
                                                       streamId=streamNum, relayCmd=3, body=b'')
            self.headerConnection.routerSocket.sendall(relayEndInfo)
            if self.headerConnection.socketLock.locked():
                self.headerConnection.socketLock.release()

            self.allDataLock.acquire()
            if (self.headerConnection.connectionKey, self.headCircuit, streamNum) in self.interfaceInfo:
                del self.interfaceInfo[(self.headerConnection.connectionKey, self.headCircuit, streamNum)]
            self.allDataLock.release()
            pass
        pass


    def processProxyMessage(self, headerMessages, data, changeFlag):
        """
        This function is used to modify the HTTP header (block persistent connection) and
        fetch web server IP and port. Finally reform the HTTP request message

        :param headerMessages: the header information as a list
        :param information: the data as a list
        :return: (hostAddress, port, result)
                 hostAddress: the ip of web server
                 port: the port number of web server
                 result: the request information after modification
        """
        result = ""  # the final result
        # the first one represent host ip in string, the second one represent port in int
        hostAddress = [None, None]
        # the method line and host line of the http header
        firstAndSecond = []
        for term in headerMessages:  # loop through each line in header
            term = term.strip()  # now all header are without "\r"
            if "GET" in term or "HEAD" in term or "POST" in term or \
               "PUT" in term or "DELETE" in term or "OPTION" in term or \
               "TRACE" in term or "PATCH" in term or "CONNECT" in term:  # mean it is the first line
                if changeFlag and term[-3:] == "1.1":  # modify the communication version
                    term = term[:-3] + "1.0"
                    pass
                self.grabHostAddress(hostAddress, term)
                firstAndSecond.append(term)
            elif re.search("host", term, re.IGNORECASE):  # this line is host line
                self.grabHostAddress(hostAddress, term)
                firstAndSecond.append(term)
            elif changeFlag and re.search("Connection", term, re.IGNORECASE):  # current line is connect method line
                term = "Connection: close"
            elif changeFlag and re.search("Proxy-connection", term, re.IGNORECASE):
                term = "Proxy-connection: close"
                pass
            result += term + "\r\n"
            pass
        # if the port is still not found,  must be identified by the header
        for term in firstAndSecond:
            if not hostAddress[1]:
                if re.search("http://", term, re.IGNORECASE):
                    hostAddress[1] = 80
                elif re.search("https://", term, re.IGNORECASE):
                    hostAddress[1] = 443
                    pass
                pass
            pass

        result += "\r\n"
        # result = result[:-2]
        result = result.encode() + data
        return (hostAddress[0], hostAddress[1], result)


    def grabHostAddress(self, hostAddress, term):
        """
        This method grab the ip address and port of web server and stored in the given array

        :param hostAddress: the given array to store the ip and port [ip, port]
        :param term: one line to grab the address information
        :return:
        """
        urlPatternA = "([a-zA-Z0-9]+|([a-zA-Z0-9][a-zA-Z0-9|-]+[a-zA-Z0-9]))"
        urlPatternB = "(\.([a-zA-Z0-9]+|([a-zA-Z0-9][a-zA-Z0-9|-]+[a-zA-Z0-9]))){1,}"
        urlPatternD = "[0-9]{1,3}.[0-9]{1,3}.[0-9]{1,3}.[0-9]{1,3}"
        urlPatternC = ":[0-9]{2,5}"
        url = re.search(urlPatternA + urlPatternB, term)  # if there is url like portion
        ipv4 = re.search(urlPatternD, term) # if there is ip like string
        if url:  # find url like part
            # print("url found is " + str(url))
            if not hostAddress[0]:  # the ip is not currently stored
                urlStrip = url.group(0)
                # print("\n%%%%% striped url is " + urlStrip + "\n")
                hostAddress[0] = socket.gethostbyname_ex(urlStrip)[-1][0]   # store the ip
            if not hostAddress[1]:  # the port is not stored
                urlAndPort = re.search(urlPatternA + urlPatternB + urlPatternC, term)
                if urlAndPort:  # if such url is post append with port
                    hostAddress[1] = int(urlAndPort.group(0).split(":")[-1])
                    pass
                pass
        elif ipv4:  # find ip like string
            if not hostAddress[0]:  # the ip is not found so far
                hostAddress[0] = ipv4.group(0)  # store the ip
            if not hostAddress[1]:  # port is not found so far
                # to see if the ip is post append with port
                ipv4AndPort = re.search(urlPatternD + urlPatternC, term)
                if ipv4AndPort:  # store the port number
                    hostAddress[1] = int(ipv4AndPort.group(0).split(":")[-1])
                    pass
                pass
            pass
        pass


    def readOnRouterSocket(self, socketInstance):
        while True:
            try:
                receivedMessage = self.receiveResponse(socketInstance.routerSocket)
                if len(receivedMessage) == 0:
                    quitMessage = "socket reader " + str(socketInstance.connectionKey) + "quit \n"
                    quitMessage += "the other side closed!"
                    logging.info(quitMessage)
                    break
                messageList = self.parseResponseMessage(receivedMessage)
                circuitNum = messageList[0]
                logging.info("In readOnRouterSocket >>> received create circuit request with circuit number " + str(circuitNum))
                if messageList[1] == 1:
                    if socketInstance.connector:
                        if circuitNum % 2 == 0:
                            check = False
                        else:
                            check = True
                    else:
                        if circuitNum % 2 == 0:
                            check = True
                        else:
                            check = False
                    if check:
                        logging.info("In readOnRouterSocket >>> cannot create circuit because violate odd even rule")
                        responseMessage = self.generateRequestMessage(commandNumber=8, circuitNumber=circuitNum)
                        socketInstance.socketLock.acquire()
                        socketInstance.routerSocket.sendall(responseMessage)
                        if socketInstance.socketLock.locked():
                            socketInstance.socketLock.release()
                        pass

                    socketInstance.dataLock.acquire()
                    if circuitNum in socketInstance.circuitTable:
                        logging.info("In readOnRouterSocket >>> cannot create circuit because already has such circuit")
                        socketInstance.dataLock.release()
                        responseMessage = self.generateRequestMessage(commandNumber=8, circuitNumber=circuitNum)
                        socketInstance.socketLock.acquire()
                        socketInstance.routerSocket.sendall(responseMessage)
                        if socketInstance.socketLock.locked():
                            socketInstance.socketLock.release()
                    else:

                        socketInstance.circuitTable[circuitNum] = CircuitWrapper(True, None, circuitNum)
                        threading.Thread(target=self.circuitEndAction, args=[socketInstance, socketInstance.circuitTable[circuitNum]]).start()
                        socketInstance.dataLock.release()
                        responseMessage = self.generateRequestMessage(commandNumber=2, circuitNumber=circuitNum)
                        socketInstance.socketLock.acquire()
                        socketInstance.routerSocket.sendall(responseMessage)
                        if socketInstance.socketLock.locked():
                            socketInstance.socketLock.release()

                        logging.info("In readOnRouterSocket >>> circuit created")
                        pass
                elif messageList[1] == 2 or messageList[1] == 8:
                    logging.info(
                        "In readOnRouterSocket >>> receive response of create circuit with number " + str(circuitNum))
                    socketInstance.requestCondition.acquire()
                    if socketInstance.currentRequest is not None and \
                       socketInstance.currentRequest['requestType'] == 1:
                        logging.info("In readOnRouterSocket >>> " + str(socketInstance.currentRequest['requestType']))
                        logging.info("In readOnRouterSocket >>> " + str(socketInstance.currentRequest['circuitNum']))
                        socketInstance.requestCondition.release()
                        # socketInstance.currentRequest['circuitNum'] == circuitNum: 'circuitNum' only valid when rel extend (tell what circuit to extend)
                        logging.info("In readOnRouterSocket >>> create circuit response match")
                        socketInstance.responseCondition.acquire()
                        socketInstance.requestResponse = True if messageList[1] == 2 else False
                    else:
                        socketInstance.requestCondition.release()
                        socketInstance.responseCondition.acquire()
                        socketInstance.requestResponse = False
                        pass
                    socketInstance.responseCondition.notify()
                    socketInstance.responseCondition.release()
                elif messageList[1] == 4:
                    logging.info("In readOnRouterSocket >>> receive destroy circuit with num " + str(circuitNum))
                    socketInstance.dataLock.acquire()
                    if circuitNum in socketInstance.circuitTable:
                        if socketInstance.circuitTable[circuitNum].isEnd:
                            socketInstance.dataLock.release()
                            logging.info("In readOnRouterSocket >>> destory head ")
                            try:
                                socketInstance.circuitTable[circuitNum].requestCondition.acquire()
                                socketInstance.circuitTable[circuitNum].terminate = True
                                socketInstance.circuitTable[circuitNum].requestCondition.notify()
                                socketInstance.circuitTable[circuitNum].requestCondition.release()
                                del socketInstance.circuitTable[circuitNum]
                            finally:
                                if socketInstance.circuitTable[circuitNum].requestLock.locked():
                                    socketInstance.circuitTable[circuitNum].release()
                                pass
                        else:
                            logging.info("In readOnRouterSocket >>> destory middle connection ")
                            nextHop = socketInstance.circuitTable[circuitNum].nextHop
                            del socketInstance.circuitTable[circuitNum]
                            socketInstance.dataLock.release()

                            self.allDataLock.acquire()
                            nextHopSocketInstance = self.connectionInfo[(nextHop[0], nextHop[1])]
                            self.allDataLock.release()
                            try:
                                nextHopSocketInstance.dataLock.acquire()
                                del nextHopSocketInstance.circuitTable[nextHop[2]]
                                nextHopSocketInstance.dataLock.release()
                                destroyInformation = self.generateRequestMessage(commandNumber=4, circuitNumber=nextHop[2])
                                nextHopSocketInstance.socketLock.acquire()
                                nextHopSocketInstance.routerSocket.sendall(destroyInformation)
                                if nextHopSocketInstance.socketLock.locked():
                                    nextHopSocketInstance.socketLock.release()
                            finally:
                                if nextHopSocketInstance.dataLock.locked():
                                    nextHopSocketInstance.dataLock.release()
                                if nextHopSocketInstance.socketLock.locked():
                                    nextHopSocketInstance.socketLock.release()
                            pass
                elif messageList[1] == 3:
                    # [cir#, command, stream#, bodyLength, relCommand, body]
                    if messageList[-2] == 6:
                        logging.info("In readOnRouterSocket >>> received relay extend command with circuit " + str(circuitNum))
                        socketInstance.dataLock.acquire()
                        if (circuitNum not in socketInstance.circuitTable) or (not socketInstance.circuitTable[circuitNum].isEnd):
                            logging.info("In readOnRouterSocket >>> cannot process relay extend")
                            socketInstance.dataLock.release()
                            failedInformation = self.generateRequestMessage(commandNumber=3, circuitNumber=circuitNum,
                                                                            streamId=0, body=b'', relayCmd=12)
                            socketInstance.socketLock.acquire()
                            socketInstance.routerSocket.sendall(failedInformation)
                            if socketInstance.socketLock.locked():
                                socketInstance.socketLock.release()
                        else:
                            logging.info("In readOnRouterSocket >>> processing relay extend")
                            circuitInstance = socketInstance.circuitTable[circuitNum]
                            socketInstance.dataLock.release()
                            # should parse body information here
                            ipAndPortAndAgent = self.parseBodyMessage(6, messageList[-1])
                            # (ip, port, agent)
                            try:
                                circuitInstance.requestCondition.acquire()
                                circuitInstance.requestBuffer.append({'requestNum':0, 'requestInfo':ipAndPortAndAgent, 'condition':None, 'response':None, 'streamNum':None})
                                circuitInstance.requestCondition.notify()
                                circuitInstance.requestCondition.release()
                            finally:
                                if circuitInstance.requestLock.locked():
                                    circuitInstance.requestLock.release()
                                pass
                    elif messageList[-2] == 7 or messageList[-2] == 12:
                        # check condition
                        logging.info("In readOnRouterSocket >>> received relay extend result with circuit " + str(circuitNum))
                        socketInstance.requestCondition.acquire()
                        if socketInstance.currentRequest is not None and \
                           socketInstance.currentRequest['requestType'] == 2 and \
                           socketInstance.currentRequest['circuitNum'] == circuitNum:
                            socketInstance.requestCondition.release()

                            socketInstance.responseCondition.acquire()
                            socketInstance.requestResponse = True if messageList[-2] == 7 else False
                            pass
                        else:
                            socketInstance.requestCondition.release()

                            socketInstance.responseCondition.acquire()
                            socketInstance.requestResponse = False
                            logging.info("In readOnRouterSocket >>> relay extend result mismatch ")
                        socketInstance.responseCondition.notify()
                        socketInstance.responseCondition.release()
                    else:
                        socketInstance.dataLock.acquire() # rememeber to unlock finally
                        if circuitNum not in socketInstance.circuitTable:
                            socketInstance.dataLock.release()
                            logging.warning("In readOnRouterSocket >>> received relay command " + str(messageList[-2])
                                            + " with unmatched circuitNum " + str(circuitNum))
                        elif not socketInstance.circuitTable[circuitNum].isEnd:
                            circuitInstance = socketInstance.circuitTable[circuitNum]
                            socketInstance.dataLock.release()

                            nextHop = circuitInstance.nextHop
                            logging.info("In readOnRouterSocket >>> redirecting relay command to " + str(nextHop))
                            self.allDataLock.acquire()
                            nextHopInstance = self.connectionInfo[(nextHop[0], nextHop[1])]
                            self.allDataLock.release()
                            redirectMessage = self.generateRequestMessage(commandNumber=messageList[1], circuitNumber=nextHop[2],
                                                                          streamId=messageList[2], relayCmd=messageList[-2], body=messageList[-1])
                            try:
                                nextHopInstance.socketLock.acquire()
                                nextHopInstance.routerSocket.sendall(redirectMessage)
                                if nextHopInstance.socketLock.locked():
                                    nextHopInstance.socketLock.release()
                            finally:
                                if nextHopInstance.socketLock.locked():
                                    nextHopInstance.socketLock.release()
                                pass
                        elif messageList[-2] == 1:
                            logging.info("In readOnRouterSocket >>> received relay begin request")
                            circuitInstance = socketInstance.circuitTable[circuitNum]
                            socketInstance.dataLock.release()
                            ipAndPort = self.parseBodyMessage(1, messageList[-1])
                            try:
                                circuitInstance.requestCondition.acquire()
                                circuitInstance.requestBuffer.append({'requestNum':1, 'requestInfo':(ipAndPort[0], ipAndPort[1], messageList[2]), 'condition':None, 'response':None, 'streamNum':messageList[2]})
                                circuitInstance.requestCondition.notify()
                                circuitInstance.requestCondition.release()
                            finally:
                                if circuitInstance.requestLock.locked():
                                    circuitInstance.requestLock.release()
                                pass
                        elif messageList[-2] == 2:
                            """
                            TODO:
                                Received relay data here
                                when implementing proxy interface, fill commands here
                                when testing, fill proper codes here
                            """
                            self.allDataLock.acquire()
                            if (socketInstance.connectionKey, circuitNum, messageList[2]) in self.interfaceInfo:
                                interfaceInstance = self.interfaceInfo[
                                    (socketInstance.connectionKey, circuitNum, messageList[2])]
                                self.allDataLock.release()
                                try:
                                    interfaceInstance.proxySocketLock.acquire()
                                    interfaceInstance.proxySocket.sendall(messageList[-1])
                                    if interfaceInstance.proxySocketLock.locked():
                                        interfaceInstance.proxySocketLock.release()
                                except socket.error as e:
                                    logging.warning(
                                        "In readOnRouterSocket >>> encounter socket error when send relay data " + str(
                                            e))
                                    if interfaceInstance.proxySocketLock.locked():
                                        interfaceInstance.proxySocketLock.release()
                                        pass
                                pass
                            else:
                                self.allDataLock.release()
                        elif messageList[-2] == 3:
                            """
                            TODO:
                                Received relay end here
                                1. terminate related thread
                                2. delete interface information
                            """
                            # self.allDataLock.acquire()
                            # if (socketInstance.connectionKey, circuitNum, messageList[2]) in self.interfaceInfo:
                            #     interfaceInstance = self.interfaceInfo[(socketInstance.connectionKey, circuitNum, messageList[2])]
                            #     # self.allDataLock.release()
                            #
                            #     try:
                            #         interfaceInstance.proxySocketLock.acquire()
                            #         interfaceInstance.proxySocket.close()
                            #         interfaceInstance.proxySocketLock.release()
                            #     except socket.error as e:
                            #         logging.warning("In readOnRouterSocket >>> encounter socket error when relay end " + str(e))
                            #         if interfaceInstance.proxySocketLock.locked():
                            #             interfaceInstance.proxySocketLock.release()
                            #             pass
                            #     # self.allDataLock.acquire()
                            #     del self.interfaceInfo[(socketInstance.connectionKey, circuitNum, messageList[2])]
                            #     self.allDataLock.release()
                            # else:
                            #     self.allDataLock.release()
                            pass
                        elif messageList[-2] == 4 or messageList[-2] == 11:
                            logging.info("In readOnRouterSocket >>> received relay begin response")
                            circuitInstance = socketInstance.circuitTable[circuitNum]
                            socketInstance.dataLock.release()
                            try:
                                circuitInstance.requestCondition.acquire()
                                if circuitInstance.currentRequest is not None and \
                                   circuitInstance.currentRequest['requestNum'] == 2 and \
                                   circuitInstance.currentRequest['streamNum'] == messageList[2]:
                                    logging.info("In readOnRouterSocket >>> relay begin response match ")
                                    circuitInstance.requestCondition.release()

                                    circuitInstance.responseCondition.acquire()
                                    circuitInstance.requestResponse = True if messageList[-2] == 4 else False
                                else:
                                    logging.info("In readOnRouterSocket >>> relay begin response mismatch ")
                                    circuitInstance.requestCondition.release()

                                    circuitInstance.responseCondition.acquire()
                                    circuitInstance.requestResponse = False
                                    pass

                                circuitInstance.responseCondition.notify()
                                circuitInstance.responseCondition.release()
                            except:
                                if circuitInstance.responseLock.locked():
                                    circuitInstance.responseLock.release()
                                pass
            except socket.error as e:
                logging.info("Encouter error in readOnRouterSocket ")
                raise e
            finally:
                if socketInstance.socketLock.locked():
                    socketInstance.socketLock.release()
                if socketInstance.dataLock.locked():
                    socketInstance.dataLock.release()
                if socketInstance.responseBufferLock.locked():
                    socketInstance.responseBufferLock.release()
                if socketInstance.requestBufferLock.locked():
                    socketInstance.requestBufferLock.release()
                if self.allDataLock.locked():
                    self.allDataLock.release()
                pass
            pass
        pass


    def circuitEndAction(self, socketInstance, circuitInstance):
        logging.info("\n\n\n\n\nIn circuitEndAction >>> circuit end processor running " + str(socketInstance.connectionKey) + " " + str(circuitInstance.circuitNumber))
        while True:
            try:
                circuitInstance.requestCondition.acquire()
                logging.info("**In circuitEndAction >>> circuit end processor ready to process  ")
                if not circuitInstance.terminate and len(circuitInstance.requestBuffer) == 0:
                    circuitInstance.requestCondition.wait()
                    pass
                logging.info("**In circuitEndAction >>> circuit end processor ready to processing " )
                if circuitInstance.terminate:
                    logging.info("**In circuitEndAction >>> circuit processor quit")
                    circuitInstance.requestCondition.release()
                    break
                circuitInstance.currentRequest = circuitInstance.requestBuffer.pop(0)
                circuitInstance.requestCondition.release()

                logMessage = "In circuitEndAction >>> processing request\n"
                for k, v in circuitInstance.currentRequest.items():
                    logMessage += k + ":" + str(v) + "\n"
                    pass
                logging.info(logMessage)

                if circuitInstance.currentRequest['requestNum'] == 0:
                    # (ip, port, agentNum)
                    relayDest = circuitInstance.currentRequest['requestInfo']
                    logging.info("In circuitEndAction >>> process relay extend request with dest " + str(relayDest))

                    if relayDest[2] == self.agentId:
                        connectionKey = (self.agentId, 1)
                    else:
                        connectionKey = (relayDest[2], 0)
                        pass
                    logging.info("In circuitEndAction >>> get next hop key " + str(connectionKey))
                    added = False
                    try:

                        self.allDataLock.acquire()

                        if connectionKey not in self.connectionInfo:
                            firstSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                            firstSocket.connect((relayDest[0], relayDest[1]))

                            openMessage = self.generateRequestMessage(commandNumber=5, circuitNumber=0, openerID=self.agentId,
                                                                      openedID=relayDest[2])
                            firstSocket.sendall(openMessage)
                            logging.info("In circuitEndAction >>> open message send")
                            self.allDataLock.release()

                            while True:
                                receivedOpen = self.receiveResponse(firstSocket)
                                returnList = self.parseResponseMessage(receivedOpen)
                                if returnList[1] == 6 or returnList[1] == 7:
                                    break
                                pass
                            circuitNum, command, openerID, openedID = returnList
                            logMessage = "In circuitEndAction >>> received response of create circuit\n"
                            logMessage += "circuitNum " + str(circuitNum) + " command " + str(command) + \
                                          " openerID " + str(openerID) + " openedID " + str(openedID)
                            logging.info(logMessage)

                            self.allDataLock.acquire()
                            if openerID == self.agentId and openedID == relayDest[2] and command == 6 \
                               and connectionKey not in self.connectionInfo:
                                self.connectionInfo[connectionKey] = SocketWrapper(firstSocket, True, connectionKey)
                                threading.Thread(target=self.readOnRouterSocket, args=[self.connectionInfo[connectionKey]]).start()
                                threading.Thread(target=self.processRouterSocketRequest, args=[self.connectionInfo[connectionKey]]).start()
                                logging.info("In circuitEndAction >>> connection entry added")
                                pass
                    except socket.error as e:
                        raise e
                    finally:
                        if not self.allDataLock.locked():
                            self.allDataLock.acquire()
                            pass
                        if connectionKey in self.connectionInfo:
                            added = True
                            if relayDest[2] == self.agentId:
                                connectionKey = (self.agentId, 0)
                            currentSocketInstance = self.connectionInfo[connectionKey]
                        if self.allDataLock.locked():
                            self.allDataLock.release()
                            pass
                        pass

                    thisConnectionKey = socketInstance.connectionKey
                    thisCircuitNum = circuitInstance.circuitNumber
                    errorMessage = "In circuitEndAction >>> Cannot extend circuit from " + str((thisConnectionKey[0], thisConnectionKey[1], thisCircuitNum)) + " to " + str(connectionKey) + "\n"
                    relayFailMessage = self.generateRequestMessage(commandNumber=3,
                                                                    circuitNumber=circuitInstance.circuitNumber,
                                                                    streamId=0, relayCmd=12, body=b'')
                    relayOkMessage = self.generateRequestMessage(commandNumber=3,
                                                                    circuitNumber=circuitInstance.circuitNumber,
                                                                    streamId=0, relayCmd=7, body=b'')
                    if not added:
                        socketInstance.socketLock.acquire()
                        socketInstance.routerSocket.sendall(relayFailMessage)
                        if socketInstance.socketLock.locked():
                            socketInstance.socketLock.release()

                        errorMessage += "First connection cannot be established!"
                        logging.warning(errorMessage)
                    else:
                        try:
                            # now we have the connection in connectionKey/currentSocketInstance
                            tempResponseLock = threading.Lock()
                            tempRequestCV = threading.Condition(tempResponseLock)
                            previousHop = (socketInstance.connectionKey[0], socketInstance.connectionKey[1], circuitInstance.circuitNumber)
                            requestInfo = {'requestType':1, 'condition':tempRequestCV, 'previousHop':previousHop, 'response':None, 'nextDest':None, 'circuitNum':None}
                            tempRequestCV.acquire()

                            currentSocketInstance.requestCondition.acquire()
                            currentSocketInstance.requestBuffer.append(requestInfo)
                            currentSocketInstance.requestCondition.notify()
                            currentSocketInstance.requestCondition.release()
                            logging.info("In circuitEndAction >>> create circuit request send")

                            tempRequestCV.wait()
                            if not requestInfo['response'][0]:
                                tempRequestCV.release()
                                logging.info("In circuitEndAction >>> cannot create circuit")
                                socketInstance.socketLock.acquire()
                                socketInstance.routerSocket.sendall(relayFailMessage)
                                if socketInstance.socketLock.locked():
                                    socketInstance.socketLock.release()

                                errorMessage += "First connection cannot be established!"
                                logging.warning(errorMessage)
                            else:
                                tempRequestCV.release()
                                socketInstance.dataLock.acquire()
                                circuitInstance.nextHop = (connectionKey[0], connectionKey[1], requestInfo['response'][1])
                                circuitInstance.isEnd = False
                                socketInstance.dataLock.release()
                                logging.info("In circuitEndAction >>> circuit created with circuit number " + str(requestInfo['response'][1]))
                                socketInstance.socketLock.acquire()
                                socketInstance.routerSocket.sendall(relayOkMessage)
                                if socketInstance.socketLock.locked():
                                    socketInstance.socketLock.release()
                                break
                            pass
                        finally:
                            if tempResponseLock.locked():
                                tempResponseLock.release()
                            if currentSocketInstance.requestBufferLock.locked():
                                currentSocketInstance.requestBufferLock.release()
                                pass
                elif circuitInstance.currentRequest['requestNum'] == 1:
                    webServerIp, webServerport, currentStreamNum = circuitInstance.currentRequest['requestInfo']
                    webSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    try:
                        webSocket.connect((webServerIp, webServerport))
                    except socket.error as e:
                        beginFailInfo = self.generateRequestMessage(commandNumber=3, circuitNumber=circuitInstance.circuitNumber,
                                                                    streamId=currentStreamNum, relayCmd=11, body=b'')
                        try:
                            socketInstance.socketLock.acquire()
                            socketInstance.routerSocket.sendall(beginFailInfo)
                            if socketInstance.socketLock.locked():
                                socketInstance.socketLock.release()
                        except socket.error as e1:
                            if socketInstance.socketLock.locked():
                                socketInstance.socketLock.release()
                                pass
                            raise e1
                        '''
                        TODO:
                            remember to change to continue!!!!!!
                        '''
                        raise e
                    webSocketLock = threading.Lock()
                    self.allDataLock.acquire()
                    self.interfaceInfo[(socketInstance.connectionKey, circuitInstance.circuitNumber, currentStreamNum)] = ProxyInterface(webSocket, webSocketLock)
                    self.allDataLock.release()
                    webSocket.settimeout(5)
                    threading.Thread(target=self.openProxyTunnel, args=[socketInstance, circuitInstance.circuitNumber,
                                                                       currentStreamNum, webSocket, webSocketLock]).start()

                    try:
                        beginFailInfo = self.generateRequestMessage(commandNumber=3,
                                                                    circuitNumber=circuitInstance.circuitNumber,
                                                                    streamId=currentStreamNum, relayCmd=4, body=b'')
                        socketInstance.socketLock.acquire()
                        socketInstance.routerSocket.sendall(beginFailInfo)
                        if socketInstance.socketLock.locked():
                            socketInstance.socketLock.release()
                    except socket.error as e:
                        raise e
                    finally:
                        if socketInstance.socketLock.locked():
                            socketInstance.socketLock.release()
                    pass
                elif circuitInstance.currentRequest['requestNum'] == 2:
                    '''
                    TODO:
                        received send relay begin request here, process it
                    '''
                    webIp, webPort = circuitInstance.currentRequest['requestInfo']
                    circuitInstance.requestCondition.acquire()
                    found = False
                    count = 0
                    while count <= (2**16):
                        currentStreamNum = circuitInstance.streamCount
                        circuitInstance.streamCount = (currentStreamNum + 1) % (2 ** 16)
                        if circuitInstance.streamCount == 0:
                            circuitInstance.streamCount = 1
                            pass
                        if (socketInstance.connectionKey, circuitInstance.circuitNumber, currentStreamNum) not in self.interfaceInfo:
                            found = True
                            break
                        count += 1
                        pass
                    if not found:
                        circuitInstance.requestCondition.release()
                        logging.info("In circuitEndAction >>> cannot found next streamNum")

                        circuitInstance.currentRequest['condition'].acquire()
                        circuitInstance.currentRequest['response'] = [False]
                        circuitInstance.currentRequest['condition'].notify()
                        circuitInstance.currentRequest['condition'].release()
                    else:
                        circuitInstance.currentRequest['streamNum'] = currentStreamNum
                        circuitInstance.requestCondition.release()
                        relayBeginBody = self.generateBodyMessage(command=1, ip=webIp, port=webPort)
                        relayBeginMessage = self.generateRequestMessage(commandNumber=3, circuitNumber=circuitInstance.circuitNumber,
                                                                        streamId=currentStreamNum, relayCmd=1, body=relayBeginBody)

                        socketInstance.socketLock.acquire()
                        socketInstance.routerSocket.sendall(relayBeginMessage)
                        if socketInstance.socketLock.locked():
                            socketInstance.socketLock.release()

                        circuitInstance.responseCondition.acquire()
                        circuitInstance.responseCondition.wait()

                        if circuitInstance.requestResponse:
                            circuitInstance.responseCondition.release()

                            circuitInstance.currentRequest['condition'].acquire()
                            circuitInstance.currentRequest['response'] = [True]
                            circuitInstance.currentRequest['condition'].notify()
                            circuitInstance.currentRequest['condition'].release()
                            pass
                        else:
                            circuitInstance.responseCondition.release()

                            circuitInstance.currentRequest['condition'].acquire()
                            circuitInstance.currentRequest['response'] = [False]
                            circuitInstance.currentRequest['condition'].notify()
                            circuitInstance.currentRequest['condition'].release()
                            pass
                    pass
            except socket.error as e:
                raise e
            finally:
                if not circuitInstance.requestLock.locked():
                    circuitInstance.requestLock.acquire()
                circuitInstance.currentRequest = None
                circuitInstance.requestLock.release()

                if socketInstance.socketLock.locked():
                    socketInstance.socketLock.release()
                if socketInstance.dataLock.locked():
                    socketInstance.dataLock.release()
                    pass
                pass
        pass


    def processRouterSocketRequest(self, socketInstance):
        while True:
            try:
                socketInstance.requestCondition.acquire()
                if not socketInstance.terminate and len(socketInstance.requestBuffer) == 0:
                    socketInstance.requestCondition.wait()
                    pass
                if socketInstance.terminate:
                    logging.info("In processRouterSocketRequest >>> router processor quit")
                    socketInstance.requestCondition.release()
                    break
                socketInstance.currentRequest = socketInstance.requestBuffer.pop(0)
                socketInstance.requestCondition.release()
                logMessage = "In processRouterSocketRequest >>> processing request\n"
                for k, v in socketInstance.currentRequest.items():
                    logMessage += k + ":" + str(v) + "\n"
                    pass
                logging.info(logMessage)

                if socketInstance.currentRequest['requestType'] == 1:
                    logging.info("In processRouterSocketRequest >>> processing send create circuit")
                    socketInstance.dataLock.acquire()
                    count = 0
                    found = False
                    while count <= (2**16):
                        if socketInstance.connector:
                            currentCircuitNum = socketInstance.circuitCount * 2 + 1
                        else:
                            currentCircuitNum = socketInstance.circuitCount * 2
                            pass
                        socketInstance.circuitCount = (socketInstance.circuitCount + 1) % (2 ** 16)
                        if socketInstance.circuitCount == 0:
                            socketInstance.circuitCount = 1
                            pass
                        if currentCircuitNum not in socketInstance.circuitTable:
                            found = True
                            break
                        count += 1

                    logging.info("In processRouterSocketRequest >>> found next circuitNum " + str(currentCircuitNum))
                    if not found:
                        raise ValueError("Could not found valid circuit number, may be used out!")

                    socketInstance.dataLock.release()
                    createCircuitRequest = self.generateRequestMessage(commandNumber=1, circuitNumber=currentCircuitNum)
                    socketInstance.socketLock.acquire()
                    socketInstance.routerSocket.sendall(createCircuitRequest)
                    if socketInstance.socketLock.locked():
                        socketInstance.socketLock.release()

                    socketInstance.responseCondition.acquire()
                    socketInstance.responseCondition.wait()
                    # the response is here
                    logging.info("In processRouterSocketRequest >>> received create circuit response ")
                    if socketInstance.requestResponse:
                        socketInstance.responseCondition.release()

                        socketInstance.dataLock.acquire()
                        socketInstance.circuitTable[currentCircuitNum] = CircuitWrapper(False, None, currentCircuitNum)
                        circuitInstance = socketInstance.circuitTable[currentCircuitNum]
                        logging.info("In processRouterSocketRequest >>> insert entry key " + str(socketInstance.connectionKey) + " circuit " + str(currentCircuitNum))
                        socketInstance.dataLock.release()

                        socketInstance.currentRequest['condition'].acquire()
                        socketInstance.currentRequest['response'] = [True, currentCircuitNum]
                        if not socketInstance.currentRequest['previousHop']:
                            circuitInstance.isEnd = True
                            threading.Thread(target=self.circuitEndAction, args=[socketInstance, circuitInstance]).start()
                            logging.info("In processRouterSocketRequest >>> this connection is head")
                            pass
                        else:
                            circuitInstance.nextHop = (socketInstance.currentRequest['previousHop'])
                            logging.info("In processRouterSocketRequest >>> next hop is " + str(circuitInstance.nextHop))
                    else:
                        logging.info("In processRouterSocketRequest >>> create circuit failed ")
                        socketInstance.responseCondition.release()
                        socketInstance.currentRequest['condition'].acquire()
                        socketInstance.currentRequest['response'] = [False]
                        pass
                    socketInstance.currentRequest['condition'].notify()
                    socketInstance.currentRequest['condition'].release()
                elif socketInstance.currentRequest['requestType'] == 2:
                    # (ip, port, agentID)
                    relayNextHop = socketInstance.currentRequest['nextDest']
                    logging.info("In processRouterSocketRequest >>> processing send relay extend command with next hop " + str(relayNextHop))
                    relayExBody = self.generateBodyMessage(command=6, ip=relayNextHop[0],
                                                           port=relayNextHop[1], agentId=relayNextHop[2])
                    relayExRequest = self.generateRequestMessage(commandNumber=3,
                                     circuitNumber=socketInstance.currentRequest['circuitNum'],
                                                                 streamId=0, relayCmd=6, body=relayExBody)
                    socketInstance.socketLock.acquire()
                    socketInstance.routerSocket.sendall(relayExRequest)
                    if socketInstance.socketLock.locked():
                        socketInstance.socketLock.release()
                    logging.info("In processRouterSocketRequest >>> relay extend message sended")
                    socketInstance.responseCondition.acquire()
                    socketInstance.responseCondition.wait()

                    if socketInstance.requestResponse:
                        logging.info("In processRouterSocketRequest >>> relay extend success")
                        socketInstance.responseCondition.release()
                        socketInstance.currentRequest['condition'].acquire()
                        socketInstance.currentRequest['response'] = [True]
                    else:
                        logging.info("In processRouterSocketRequest >>> relay extend fail")
                        socketInstance.responseCondition.release()
                        socketInstance.currentRequest['condition'].acquire()
                        socketInstance.currentRequest['response'] = [False]
                        pass
                    socketInstance.currentRequest['condition'].notify()
                    socketInstance.currentRequest['condition'].release()
            except socket.error as e:
                raise e
            finally:
                if not socketInstance.requestBufferLock.locked():
                    socketInstance.requestBufferLock.acquire()
                    pass
                socketInstance.currentRequest = None
                socketInstance.requestCondition.release()

                if socketInstance.responseBufferLock.locked():
                    socketInstance.responseBufferLock.release()
                    pass
                if socketInstance.dataLock.locked():
                    socketInstance.dataLock.release()
                    pass
                if socketInstance.socketLock.locked():
                    socketInstance.socketLock.release()
                    pass
                pass
            pass
        pass


    def acceptRouterConnection(self):
        self.connectionSocket.listen(5)
        while True:
            try:
                clientSocket, clientAddr = self.connectionSocket.accept()
                logging.info("In acceptRouterConnection >>> connection request in coming")
                openMessage = self.receiveResponse(clientSocket)
                messageList = self.parseResponseMessage(openMessage)

                if messageList[1] != 5:
                    clientSocket.close()
                    continue
                if messageList[3] != self.agentId:
                    refuseMessage = self.generateRequestMessage(commandNumber=7, circuitNumber=0, openedID=self.agentId,
                                                                openerID=messageList[2])
                    clientSocket.sendall(refuseMessage)
                    clientSocket.close()
                    logging.info("In acceptRouterConnection >>> Received miss matched open connection with opener "
                                 + str(messageList[2]) + " and opened " + str(messageList[3]))
                    continue

                connectionKey = (messageList[2], 0)

                self.allDataLock.acquire()
                if connectionKey not in self.connectionInfo:
                    openedMessage = self.generateRequestMessage(commandNumber=6, circuitNumber=0,
                                                                openerID=messageList[2], openedID=self.agentId)
                    clientSocket.sendall(openedMessage)
                    self.connectionInfo[connectionKey] = SocketWrapper(clientSocket, False, connectionKey)
                    threading.Thread(target=self.readOnRouterSocket, args=[self.connectionInfo[connectionKey]]).start()
                    threading.Thread(target=self.processRouterSocketRequest, args=[self.connectionInfo[connectionKey]]).start()

                    logging.info("In acceptRouterConnection >>> Connection created with opener " + str(messageList[2]) + " opened " + str(messageList[3]))
                else:
                    self.allDataLock.release()
                    refuseMessage = self.generateRequestMessage(commandNumber=7, circuitNumber=0, openedID=self.agentId,
                                                                openerID=messageList[2])
                    clientSocket.sendall(refuseMessage)
                    clientSocket.close()
                    logging.info("In acceptRouterConnection >>> such connection already exist")
            except socket.error as e:
                raise e
            finally:
                if self.allDataLock.locked():
                    self.allDataLock.release()
                    pass
                pass
            pass
        pass


    def startUpCircuit(self):
        errorMessage = "Connection to other routers cannot be established\n"

        returnState, returnData = self.registrationHelper(3)  # fetch registered router

        if not returnState:
            errorMessage += "Cannot fetch information from registration server in step0"
            raise Exception(errorMessage)

        logging.info("In startUpCircuit >>> fetch data success, size " + str(returnData))
        added = False
        while returnData:  # repeat this step until success or there is no option left
            try:
                index = randint(0, len(returnData) - 1)
                picked = returnData[index]

                logging.info("In startUpCircuit >>> The agent picked is " + str(picked[2]))
                logging.info("In startUpCircuit >>> IP " + str(picked[0]) + " port " + str(picked[1]))

                if picked[2] == self.agentId:
                    connectionKey = (self.agentId, 1)
                else:
                    connectionKey = (picked[2], 0)
                    pass

                self.allDataLock.acquire()
                if connectionKey not in self.connectionInfo:
                    firstSocket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    firstSocket.connect((picked[0], picked[1]))

                    openMessage = self.generateRequestMessage(commandNumber=5, circuitNumber=0, openerID=self.agentId,
                                                              openedID=picked[2])
                    firstSocket.sendall(openMessage)
                    logging.info("In startUpCircuit >>> open message send")
                    self.allDataLock.release()

                    while True:
                        receivedOpen = self.receiveResponse(firstSocket)
                        returnList = self.parseResponseMessage(receivedOpen)
                        if returnList[1] == 6 or returnList[1] == 7:
                            break
                        pass
                    circuitNum, command, openerID, openedID = returnList
                    logMessage = "In startUpCircuit >>> received response of create circuit\n"
                    logMessage += "circuitNum " + str(circuitNum) + " command " + str(command) + \
                                  " openerID " + str(openerID) + " openedID " + str(openedID)
                    logging.info(logMessage)
                    self.allDataLock.acquire()

                    if openerID == self.agentId and openedID == picked[2] and command == 6 and \
                                    connectionKey not in self.connectionInfo:
                        self.connectionInfo[connectionKey] = SocketWrapper(firstSocket, True, connectionKey)
                        threading.Thread(target=self.readOnRouterSocket, args=[self.connectionInfo[connectionKey]]).start()
                        threading.Thread(target=self.processRouterSocketRequest, args=[self.connectionInfo[connectionKey]]).start()
                        logging.info("In startUpCircuit >>> connection entry added")
                        pass
                    else:
                        logging.info("In startUpCircuit >>> connection entry not added")
            except socket.error as e:
                raise e
            finally:
                if not self.allDataLock.locked():
                    self.allDataLock.acquire()
                    pass
                if connectionKey not in self.connectionInfo:
                    del returnData[index]
                    self.allDataLock.release()
                    logging.info("In startUpCircuit >>> connection information cannot be used")
                else:
                    added = True
                    socketInstance = self.connectionInfo[connectionKey]
                    self.headerConnection = socketInstance
                    self.allDataLock.release()
                    logging.info("In startUpCircuit >>> connection information found!\nkey " + str(connectionKey))
                    break
                pass
            pass

        if not added:
            errorMessage += "The first connection cannot be established!\n"
            errorMessage += "Or there is no choice for second connection!"
            raise ValueError(errorMessage)

        responseLock = threading.Lock()
        responseCV = threading.Condition(responseLock)
        requestInfo = {'requestType':1, 'condition':responseCV, 'previousHop':None, 'response':None, 'nextDest':None, 'circuitNum':None}
        responseCV.acquire()

        socketInstance.requestCondition.acquire()
        socketInstance.requestBuffer.append(requestInfo)
        socketInstance.requestCondition.notify()
        socketInstance.requestCondition.release()
        logging.info("In startUpCircuit >>> create circuit request added")

        responseCV.wait()

        logging.info("In startUpCircuit >>> received response of create circuit")
        if not requestInfo['response'][0]:
            responseCV.release()
            errorMessage += "The first circuit cannot be created!"
            raise ValueError(errorMessage)
        self.headCircuit = requestInfo['response'][1]
        responseCV.release()

        logging.info("In startUpCircuit >>> The first circuit is established with circuitNum " + str(self.headCircuit))

        returnState, returnData = self.registrationHelper(3)  # fetch registered router

        if not returnState:
            errorMessage += "Cannot fetch information from registration server in step1"
            raise Exception(errorMessage)

        added = False
        while returnData:
            index = randint(0, len(returnData) - 1)
            picked = returnData[index]

            logging.info("In startUpCircuit >>> The next hop picked for relay extend is " + str(picked[2]))

            requestInfo ={'requestType':2, 'condition':responseCV, 'previousHop':None, 'nextDest':tuple(picked), 'response':None, 'circuitNum':self.headCircuit}
            responseCV.acquire()

            socketInstance.requestCondition.acquire()
            socketInstance.requestBuffer.append(requestInfo)
            socketInstance.requestCondition.notify()
            socketInstance.requestCondition.release()
            logging.info("In startUpCircuit >>> relay extend request added ")

            responseCV.wait()
            if not requestInfo['response'][0]:
                responseCV.release()
                del returnData[index]
                logging.info("In startUpCircuit >>> relay extend fail with connection " + str(picked))
            else:
                added = True
                responseCV.release()
                logging.info("In startUpCircuit >>> relay extend succcess with connection " + str(picked))
                break
            pass

        if not added:
            errorMessage += "Last hop cannot be established!"
            raise ValueError(errorMessage)
        pass

    def parseBodyMessage(self, command, body):
        state = 0
        ip = ""
        port = ""
        agentNum = b''
        for term in body:
            if state == 0 and term == 58:
                state = 1
            elif state == 1 and term == 0:
                state = 2
            elif state == 0:
                ip += chr(term)
            elif state == 1:
                port += chr(term)
            elif state == 2:
                agentNum += int(term).to_bytes(1, 'big')
                pass
            pass
        if command == 1:
            return (ip, int(port))
        elif command == 6:
            return (ip, int(port), int.from_bytes(agentNum, 'big'))


    def generateBodyMessage(self, command, ip=None, data=None, port=None, agentId=None):
        result = None
        if command == 1 or command == 6:
            result = ip + ":" + str(port)
            result = result.encode('ascii')
            result += '\x00'.encode('ascii')
            if command == 6:
                result += agentId.to_bytes(4, 'big')
                pass
        elif command == 2:
            result = data.encode('ascii')
            pass
        return result


    def registrationHelper(self, commandNum, portNum=None, serviceData=-1, serviceName=""):
        retry = 5
        returnState = False
        returnData = None

        while not returnState and retry >= 0:
            if commandNum == 1:
                returnState, returnData = self.registerAgent.getRequest(1, portNum, serviceData, serviceName)
            elif commandNum == 3:
                # returnData -> [(ip, port, data), ...]
                returnState, returnData = self.registerAgent.getRequest(3)
            elif commandNum == 5:
                returnState, returnData = self.registerAgent.getRequest(5, portNum)
            retry -= 1
            pass
        return (returnState, returnData)


    def parseResponseMessage(self, message):
        result = []
        result.append(int.from_bytes(message[:2], 'big'))
        message = message[2:]
        result.append(int.from_bytes(message[:1], 'big'))
        message = message[1:]
        if result[-1] == 5 or result[-1] == 6 or result[-1] == 7:
            result.append(int.from_bytes(message[:4], 'big'))
            message = message[4:]
            result.append(int.from_bytes(message[:4], 'big'))
            # result [cir#, command, openerID, openedID]
        elif result[-1] == 3:
            result.append(int.from_bytes(message[:2], 'big'))
            message = message[2:]
            message = message[2:]
            message = message[4:]
            bodyLength = int.from_bytes(message[:2], 'big')
            message = message[2:]
            result.append(bodyLength)
            result.append(int.from_bytes(message[:1], 'big'))
            message = message[1:]
            result.append(message[:bodyLength])
            # result [cir#, command, stream#, bodyLength, relCommand, body]
        return result


    def receiveResponse(self, givenSocket):
        receivedInfo = b''
        while True:
            addOn = givenSocket.recv(self.cellLength)
            receivedInfo += addOn
            if len(receivedInfo) >= self.cellLength or len(addOn) == 0:
                break
            pass
        return receivedInfo


    def generateRequestMessage(self, commandNumber, circuitNumber, openerID=None, openedID=None,
                               streamId=None, relayCmd=None, body=None):
        """


        :param commandNumber:
        :param circuitNumber:
        :param openerId:
        :param openedId:
        :param streamId:
        :param relayCmd:
        :param body: NEED TO BE BYTE !!!!!
        :return:
        """
        result = circuitNumber.to_bytes(2, 'big')
        result += commandNumber.to_bytes(1, 'big')
        if commandNumber == 3:
            result += streamId.to_bytes(2, 'big')
            result += int(0).to_bytes(2, 'big')
            result += int(0).to_bytes(4, 'big')
            result += len(body).to_bytes(2, 'big')
            result += relayCmd.to_bytes(1, 'big')
            result += body
        elif commandNumber == 5 or commandNumber == 6 or commandNumber == 7:
            result += openerID.to_bytes(4, 'big')
            result += openedID.to_bytes(4, 'big')
            pass
        result += '\x00'.encode('ascii') * (self.cellLength - len(result))
        return result


def main(args):
    registerAgent = RegisterAgent(R_PORT, R_SERVER_IP, R_SERVER_PORT)
    router = TorRouter(registerAgent, T_CONNECT_PORT, int(sys.argv[3]), int(sys.argv[1]), int(sys.argv[2]))
    router.runRouter()
    pass



if __name__ == '__main__':
    # parser = argparse.ArgumentParser(
        # description='Tor Router for mediate HTTP requests')

    # parser.add_argument('--rPort', action='store', default=R_PORT, dest='rPort', type=int)

    # parser.add_argument('--rServerIP', action='store', default=R_SERVER_IP, dest='rServerIP', type=str)

    # parser.add_argument('--rServerPort', action='store', default=R_SERVER_PORT, dest='rServerPort', type=int)

    # parser.add_argument('--connectPort', action='store', default=T_CONNECT_PORT, dest='connectPort', type=int)

    # parser.add_argument('--proxyPort', action='store', default=PROXY_PORT, dest='proxyPort', type=int)

    # args = parser.parse_args()

    main(sys.argv)
    pass
