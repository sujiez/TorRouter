import sys
from registerAgent import RegisterAgent

commands = {"r": 1, "f": 3, "u": 5, "p": 6, "q": 8}


def main():
    if len(sys.argv) < 3:
        usage()

    agent = RegisterAgent(int(sys.argv[3]), sys.argv[1], int(sys.argv[2]))
    agent.startRegister()
    print("Enter r(egister), u(nregister), f(etch), p(robe), or q(uit): ")

    while True:
        try:
            line = input()

            arguments = line.split(" ")
            if len(arguments) < 1:
                continue

            args = []
            args.append(commands[arguments[0]])

            if args[0] is 8:
                break
            # port = None
            if len(arguments) > 1:
                args.append(int(arguments[1]))
            # service_data = -1
            if len(arguments) > 2:
                args.append(int(arguments[2]))
            # service_name = ""
            if len(arguments) > 3:
                args.append(arguments[3])

            agent.getRequest(*args)

            pass
        except EOFError:
            # should quit
            # agent.getRequest(8, None, 0, "")
            break
        print("Enter r(egister), u(nregister), f(etch), p(robe), or q(uit): ")
        pass
    agent.closeAgent()
    print("Agent exists \n")
    pass


def usage():
    raise ValueError("python3 main_program.py agentPort serverIp serverPort <urlForCheckingIp> <agentIp> \n")
    pass


if __name__ == '__main__':
    main()
