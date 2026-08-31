#region IMPORTS
import psutil
import select
import termios
import tty
import subprocess
import re
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor
from scapy.all import sniff, TCP, IP, IPv6
from collections import deque
from rich import print
from rich.panel import Panel
from rich.live import Live
from rich.layout import Layout
import time
import rich.console
import sys
import threading
import textwrap
from rich.markup import escape
import macgputils
#endregion

#region GLOBAL STATE
lastNetSent = 0
lastNetRecv = 0
lastUploadSpeed = 0
lastDownloadSpeed = 0
uploadHistory = deque(maxlen=59)
downloadHistory = deque(maxlen=59)

cpuHistory = deque(maxlen=28)

netGraphMax = 1024 * 50
graphBars = " ▁▂▃▄▅▆▇█"

lastNetTime = time.time()
lastCpuUsage = 0

gpuText = "LOADING GPU.."
console = rich.console.Console()

packetLines = deque(maxlen=30)
lastPacket = None
lastPacketCount = 0

showUnknown = False

client = OpenAI()
chatHistory = []
currentInput = ""
chatScroll = 0
#endregion

#region HELPER FUNCTIONS
def bar(percent):
    count = percent / 10
    filled = round(count) * "█"
    return filled

def fullbar(percent):
    fullbars = bar(percent)
    remainingBars = 10 - (percent / 10)
    remBarsChar = round(remainingBars) * "░"
    fullBarsChar = fullbars + remBarsChar
    return fullBarsChar

def replaceLines(lines):
    sys.stdout.write(f"\x1b[{lines}A\x1b[J")
    sys.stdout.flush()

def showCursor():
    sys.stdout.write("\x1b[?25h")
    sys.stdout.flush()

def hideCursor():
    sys.stdout.write("\x1b[?25l")
    sys.stdout.flush()

def dotBlock(percent, width=24):
    filled = round((percent / 100) * width)
    return "▪" * filled

def spacerLength(label, bar):
    labelWidth = len(label)
    barWidth = len(bar)
    spacerLen = (57 - labelWidth) - barWidth
    spacerChars = "-" * max(0, spacerLen)
    return spacerChars
#endregion

#region NETWORK
def formatBytes(num):
    if num < 1024:
        return f"{num:.0f} B"
    elif num < 1024 * 1024:
        return f"{num / 1024:.2f} KB"
    else:
        return f"{num / (1024 * 1024):.2f} MB"

def makeNetGraph2Lines(history):
    topLine = ""
    bottomLine = ""

    for value in history:
        percent = min(value / netGraphMax, 1)
        level = int(percent * 16)

        if level <= 8:
            topLine += " "
            bottomLine += graphBars[level]
        else:
            topLine += graphBars[level - 8]
            bottomLine += "█"

    return topLine, bottomLine

def networkStats():
    global lastNetSent, lastNetRecv, lastNetTime
    global lastUploadSpeed, lastDownloadSpeed

    try:
        net = psutil.net_io_counters(pernic=True)["en0"]
    except KeyError:
        return "[red]Network interface en0 not found[/red]"

    now = time.time()
    timeDiff = now - lastNetTime

    sentDiff = net.bytes_sent - lastNetSent
    recvDiff = net.bytes_recv - lastNetRecv

    if timeDiff > 0:
        if sentDiff > 0:
            lastUploadSpeed = sentDiff / timeDiff
        if recvDiff > 0:
            lastDownloadSpeed = recvDiff / timeDiff

    lastNetSent = net.bytes_sent
    lastNetRecv = net.bytes_recv
    lastNetTime = now

    totalSent = round(net.bytes_sent / (1024 ** 2))
    totalRecv = round(net.bytes_recv / (1024 ** 2))

    try:
        ipaddress = psutil.net_if_addrs()["en0"][1].address
    except Exception:
        ipaddress = "unknown"

    try:
        activeconncount = len(psutil.net_connections(kind="inet"))
    except Exception:
        activeconncount = 0

    uploadHistory.append(lastUploadSpeed)
    downloadHistory.append(lastDownloadSpeed)

    uploadTop, uploadBottom = makeNetGraph2Lines(uploadHistory)
    downloadTop, downloadBottom = makeNetGraph2Lines(downloadHistory)

    uploadLabel = f"↑ {formatBytes(lastUploadSpeed)} | {totalSent} MiB  "
    downloadLabel = f"↓ {formatBytes(lastDownloadSpeed)} | {totalRecv} MiB"

    return (
        f"[bold red]{uploadTop}[/bold red]\n"
        f"[bold red]{uploadBottom}[/bold red]\n"
        f"[bold cyan]{downloadTop}[/bold cyan]\n"
        f"[bold cyan]{downloadBottom}[/bold cyan]\n\n"
        f"[bold red]{uploadLabel}[/bold red]"
        f"[bold cyan]{downloadLabel}[/bold cyan] \n\n"
        f"[green]Misc info:[/green]\n"
        f"[blue]Connection type: [/blue]en0 / Wi-Fi\n"
        f"[red]Local IP Address: [/red]{ipaddress}\n"
        f"[yellow]Active Connection Count: [/yellow]{activeconncount}\n"
    )
#endregion

#region CHATBOT
def askAI(message):
    try:
        response = client.responses.create(
            model="gpt-4.1-mini",
            input=message
        )
        return response.output_text
    except Exception as e:
        return f"AI ERROR: {type(e).__name__}: {e}"

def sendChatMessage(userMessage):
    global chatScroll

    chatHistory.append("AI: ")
    aiIndex = len(chatHistory) - 1
    collected = ""

    try:
        with client.responses.stream(
            model="gpt-4.1-mini",
            input=userMessage
        ) as stream:
            for event in stream:
                if event.type == "response.output_text.delta":
                    collected += event.delta
                    chatHistory[aiIndex] = f"AI: {escape(collected)}"

            stream.get_final_response()

    except Exception as e:
        chatHistory[aiIndex] = f"AI ERROR: {type(e).__name__}: {escape(str(e))}"

    chatScroll = 0

def wrappedChatLines():
    panelWidth = max(20, console.size.width // 3 - 8)
    lines = []

    for message in chatHistory:
        wrapped = textwrap.wrap(
            message,
            width=panelWidth,
            replace_whitespace=False,
            drop_whitespace=False
        )

        if wrapped:
            lines.extend(wrapped)
        else:
            lines.append("")

    return lines

def chatPanel():
    global chatScroll

    visibleHeight = 18

    lines = wrappedChatLines()
    maxScroll = max(0, len(lines) - visibleHeight)

    chatScroll = min(chatScroll, maxScroll)

    start = max(0, len(lines) - visibleHeight - chatScroll)
    end = max(0, len(lines) - chatScroll)

    visibleLines = lines[start:end]
    text = "\n".join(visibleLines)

    if chatScroll > 0:
        text = "[grey50]↑ older lines[/grey50]\n" + text

    return text + "\n\n[blue]> [/blue]" + escape(currentInput)

def handleChatInput():
    global currentInput, chatScroll

    while select.select([sys.stdin], [], [], 0)[0]:
        key = sys.stdin.read(1)

        if key in ("\n", "\r"):
            message = currentInput.strip()
            currentInput = ""

            if message:
                chatHistory.append(f"[blue]You: [/blue]{escape(message)}")
                threading.Thread(
                    target=sendChatMessage,
                    args=(message,),
                    daemon=True
                ).start()

        elif key == "\x7f":
            currentInput = currentInput[:-1]

        elif key == "[":
            maxScroll = max(0, len(wrappedChatLines()) - 18)
            chatScroll = min(chatScroll + 1, maxScroll)

        elif key == "]":
            chatScroll = max(0, chatScroll - 1)

        else:
            currentInput += key
#endregion

#region CPU STATS
def cpuGraph():
    graph = ""

    for value in cpuHistory:
        percent = min(value / 100, 1)
        level = int(percent * 8)
        graph += graphBars[level]

    return graph

def cpuStats():
    global lastCpuUsage

    cpuUsage = psutil.cpu_percent(interval=None)
    cpuCoresUsage = psutil.cpu_percent(interval=None, percpu=True)
    times = psutil.cpu_times_percent(percpu=True)

    if cpuUsage > 0:
        lastCpuUsage = cpuUsage
    else:
        cpuUsage = lastCpuUsage

    cpuHistory.append(lastCpuUsage)

    text = "CPU USAGE:  " + "[red]" + str(fullbar(cpuUsage)) + "[/red]" + str(cpuUsage) + "%"
    text += "[red]" + cpuGraph() + "[/red]" + "\n\n"

    for i, usage in enumerate(cpuCoresUsage):
        core = times[i]

        text += (
            "[white]"
            + "Core "
            + str(i + 1)
            + " Usage:  "
            + "[/white]"
            + "[red]"
            + str(fullbar(usage))
            + "[/red]"
            + "[white]"
            + str(usage)
            + "%  "
            + "[/white]"
            + "[white]"
            + "[#8fb6d8]"
            + " U:"
            + "[/#8fb6d8]"
            + str(core.user)
            + "  "
            + "[#9fca8c]"
            + "S:"
            + "[/#9fca8c]"
            + str(core.system)
            + "  "
            + "[#b6a6c8]"
            + "I:"
            + "[/#b6a6c8]"
            + str(core.idle)
            + "  "
            + "[/white]"
            + "\n"
        )

    return text
#endregion

#region GPU STATS
def gpuStats():
    global gpuText

    try:
        result = subprocess.run(
            ["powermetrics", "--samplers", "gpu_power", "-n", "1"],
            capture_output=True,
            text=True,
            timeout=8
        )

        stats = macgputils.get_gpu_stats()

        if isinstance(stats, list):
            stats = stats[0]

        gpuPower = float(stats.get("GPU Power", "0.0 mW").replace("mW", " ").strip())
        gpuAR = float(stats.get("HW Active Residency", "0.0%").replace("%", " ").strip())
        gpuIR = float(stats.get("Idle Residency", "0.0%").replace("%", " ").strip())
        gpuFreq = float(stats.get("Active Frequency", "0.0 MHz").replace("MHz", " ").strip())

        output = result.stdout
        match = re.search(r"GPU HW active residency:\s+([\d.]+)%", output)

        activeBar = f"{gpuAR:.1f}% " + dotBlock(gpuAR)
        idleBar = f"{gpuIR:.1f}% " + dotBlock(gpuIR)

        if match:
            gpuUsage = float(match.group(1))
            barText = f"{gpuUsage:.1f}%" + dotBlock(gpuUsage)

            labelText = "GPU USAGE:"
            powerText = f"{gpuPower:.0f} mW"
            freqText = f"{gpuFreq:.0f} MHz"

            gpuText = (
                f"Basic GPU stats\n"
                f"[green]{labelText} [/green]{spacerLength(labelText, barText)} [green]{barText}[/green]\n"
                f"[yellow]Power Draw: [/yellow]{spacerLength('Power Draw:', powerText)}[yellow] {gpuPower:.0f} mW [/yellow] \n"
                f"[cyan]Frequency: [/cyan]{spacerLength('Frequency:', freqText)} [cyan]{gpuFreq:.0f} MHz [/cyan] \n\n"
                f"Residency stats\n"
                f"[blue]Active: [/blue]{spacerLength('Active:', activeBar)} [blue]{activeBar}[/blue] \n"
                f"[red]Idle: [/red]{spacerLength('Idle:', idleBar)} [red]{idleBar}[/red] \n"
            )
        else:
            gpuText = "[yellow]GPU usage unavailable[/yellow]"

    except Exception as e:
        gpuText = f"[red]GPU STATS UNAVAILABLE[/red]\n[grey50]{type(e).__name__}: {e}[/grey50]"

    time.sleep(0.1)

def gpuLoop():
    while True:
        gpuStats()
        time.sleep(1)
#endregion

#region MEM + DISK STATS
def memdeskStats():
    disk = psutil.disk_usage("/")
    mem = psutil.virtual_memory()

    totalRam = mem.total / (1024 ** 3)
    usedRam = mem.used / (1024 ** 3)
    avalibleRam = mem.available / (1024 ** 3)
    freeRam = mem.free / (1024 ** 3)

    usedPercent = mem.percent
    avaliblePercent = (mem.available / mem.total) * 100
    freePercent = (mem.free / mem.total) * 100

    cachedRam = getattr(mem, "cached", 0)

    if cachedRam == 0:
        cachedRam = mem.available - mem.free

    cachedRam = cachedRam / (1024 ** 3)
    cachedPercent = (cachedRam / totalRam) * 100

    diskTotal = disk.total / (1024 ** 3)
    diskUsed = disk.used / (1024 ** 3)
    diskFree = disk.free / (1024 ** 3)
    diskFreePercent = 100 - disk.percent

    text = " "

    text += f"\n[bold white]RAM Total:[/bold white] {totalRam:.1f} GiB\n"

    usedBarText = f"{usedPercent:.0f}% " + dotBlock(usedPercent)
    usedRamText = f"Used: {usedRam:.1f} GiB"
    text += f"[red]Used:[/red] {usedRam:.1f} GiB {spacerLength(usedRamText, usedBarText)}"
    text += f"[red]{usedPercent:.0f}% {dotBlock(usedPercent)}[/red]\n"

    avalibleBarText = f"{avaliblePercent:.1f}% " + dotBlock(avaliblePercent)
    avalibleRamText = f"Avalible: {avalibleRam:.0f} GiB"
    text += f"[yellow]Avalible:[/yellow] {avalibleRam:.1f} GiB {spacerLength(avalibleRamText, avalibleBarText)}"
    text += f"[yellow]{avaliblePercent:.0f}% {dotBlock(avaliblePercent)}[/yellow]\n"

    cachedBarText = f"{cachedPercent:.0f}% " + dotBlock(cachedPercent)
    cachedRamText = f"Cached: {cachedRam:.1f} GiB"
    text += f"[cyan]Cached:[/cyan] {cachedRam:.1f} GiB {spacerLength(cachedRamText, cachedBarText)}"
    text += f"[cyan]{cachedPercent:.0f}% {dotBlock(cachedPercent)}[/cyan]\n"

    freeBarText = f"{freePercent:.0f}% " + dotBlock(freePercent)
    freeRamText = f"Free: {freeRam:.1f} GiB"
    text += f"[green]Free:[/green] {freeRam:.1f} GiB {spacerLength(freeRamText, freeBarText)}"
    text += f"[green]{freePercent:.0f}% {dotBlock(freePercent)}[/green]\n\n"

    text += f"[bold white]DISK Total:[/bold white] {diskTotal:.0f} GiB\n"

    diskUsedText = f"Used: {diskUsed:.0f}/{diskTotal:.0f} GiB"
    diskUsedBar = f"{disk.percent:.0f}% " + dotBlock(disk.percent)

    text += f"[red]Used:[/red] {diskUsed:.0f}/{diskTotal:.0f} GiB {spacerLength(diskUsedText, diskUsedBar)}"
    text += f"[red]{diskUsedBar}[/red]\n"

    diskFreeText = f"Avalible: {diskFree:.0f}/{diskTotal:.0f} GiB"
    diskFreeBar = f"{diskFreePercent:.0f}% " + dotBlock(diskFreePercent)

    text += f"[yellow]Avalible:[/yellow] {diskFree:.0f}/{diskTotal:.0f} GiB {spacerLength(diskFreeText, diskFreeBar)}"
    text += f"[yellow]{diskFreeBar}[/yellow]\n"

    return text
#endregion

#region PROCESSES STATS
def basicProcesses():
    processes_with_cpu = []

    for p in psutil.process_iter(["pid", "name"]):
        try:
            p.cpu_percent(None)
            p.memory_percent(memtype="rss")
            processes_with_cpu.append(p)
        except psutil.Error:
            pass

    final_processes = []

    for p in processes_with_cpu:
        try:
            cpu = p.cpu_percent(None)
            mem = p.memory_percent(memtype="rss")
            final_processes.append((p, cpu, mem))
        except psutil.Error:
            pass

    final_processes.sort(key=lambda item: item[1], reverse=True)

    text = ""

    for process, cpu, mem in final_processes[:22]:
        try:
            label = str(process.name())
        except psutil.Error:
            continue

        plainBar = "CPU: " + str(cpu) + "% MEM: " + str(round(mem, 3)) + "%"
        coloredBar = (
            "[red]CPU: [/red]"
            + str(cpu)
            + "% "
            + "[#6f89a8]MEM: [/]"
            + str(round(mem, 3))
            + "%"
        )

        text += label + " " + spacerLength(label, plainBar) + " " + coloredBar + "\n"

    return text

def programNameFromPort(port):
    try:
        connections = psutil.net_connections(kind="inet")
    except Exception:
        return "unknown"

    for conn in connections:
        try:
            if conn.pid and conn.laddr and conn.laddr.port == port:
                return psutil.Process(conn.pid).name()
        except Exception:
            pass

    return "unknown"
#endregion

#region PACKET STATS
def addPacketLine(packetText):
    global lastPacket, lastPacketCount

    if packetText == lastPacket:
        lastPacketCount += 1

        if packetLines:
            packetLines.pop()

        packetLines.append(str(lastPacketCount) + "x " + packetText)

    else:
        lastPacket = packetText
        lastPacketCount = 1
        packetLines.append(packetText)

def handlePacket(packet):
    if TCP not in packet:
        return

    sport = packet[TCP].sport
    dport = packet[TCP].dport

    program = programNameFromPort(sport)

    if showUnknown is False:
        if program == "unknown":
            return

    if program == "unknown":
        program = programNameFromPort(dport)

    if IP in packet:
        src = packet[IP].src
        dst = packet[IP].dst
        ipType = "IP"
    elif IPv6 in packet:
        src = packet[IPv6].src
        dst = packet[IPv6].dst
        ipType = "IPv6"
    else:
        src = "?"
        dst = "?"
        ipType = "?"

    flags = str(packet[TCP].flags)

    packetText = (
        f"[red]{program}[/red] | "
        f"[red]{sport}→{dport}[/red] | "
        f"[grey50]Ether[/grey50] / "
        f"[white]{ipType}[/white] / "
        f"[red]TCP[/red] | "
        f"[grey50]{src}:{sport}[/grey50] > "
        f"[white]{dst}:{dport}[/white] "
        f"[red]{flags}[/red]"
    )

    addPacketLine(packetText)

def startSniffing():
    sniff(iface="en0", filter="tcp", prn=handlePacket, store=False)

def packetFiltering():
    if not packetLines:
        return "Waiting for packets"

    visibleLines = list(packetLines)[-30:]
    return "\n".join(visibleLines)
#endregion

#region MAIN APP
def main():
    global showUnknown
    global lastNetSent, lastNetRecv, lastNetTime

    try:
        netStart = psutil.net_io_counters(pernic=True)["en0"]
        lastNetSent = netStart.bytes_sent
        lastNetRecv = netStart.bytes_recv
        lastNetTime = time.time()
    except KeyError:
        print("[red]Could not find network interface en0.[/red]")

    if input("Show unknown packets? (Y or N) ") == "Y":
        showUnknown = True
    else:
        showUnknown = False

    gpuThread = threading.Thread(target=gpuLoop, daemon=True)
    gpuThread.start()

    snifferThread = threading.Thread(target=startSniffing, daemon=True)
    snifferThread.start()

    layout = Layout()

    layout.split_row(
        Layout(name="left"),
        Layout(name="center"),
        Layout(name="right")
    )

    layout["left"].split_column(
        Layout(name="upperleft", ratio=1),
        Layout(name="lowerleft", ratio=3),
    )

    layout["center"].split_column(
        Layout(name="upper", ratio=15),
        Layout(name="middle", ratio=13),
        Layout(name="lower", ratio=24),
    )

    layout["right"].split_column(
        Layout(name="upperright", ratio=1),
        Layout(name="lowerright", ratio=3),
    )

    oldSettings = termios.tcgetattr(sys.stdin)
    tty.setcbreak(sys.stdin.fileno())

    try:
        with Live(layout, refresh_per_second=24, screen=True) as live:
            with ThreadPoolExecutor(max_workers=7):
                lastCpuUpdate = 0
                lastProcUpdate = 0
                lastNetUpdate = 0
                lastMemUpdate = 0
                lastPktUpdate = 0

                chat_data = ""
                cpu_data = ""
                proc_data = ""
                net_data = ""
                mem_data = ""
                pkt_data = ""

                while True:
                    handleChatInput()

                    chat_data = chatPanel()
                    now = time.time()

                    if now - lastCpuUpdate > 0.25:
                        cpu_data = cpuStats()
                        lastCpuUpdate = now

                    if now - lastProcUpdate > 1.0:
                        proc_data = basicProcesses()
                        lastProcUpdate = now

                    if now - lastNetUpdate > 0.25:
                        net_data = networkStats()
                        lastNetUpdate = now

                    if now - lastMemUpdate > 1.0:
                        mem_data = memdeskStats()
                        lastMemUpdate = now

                    if now - lastPktUpdate > 0.1:
                        pkt_data = packetFiltering()
                        lastPktUpdate = now

                    layout["upperleft"].update(
                        Panel(
                            mem_data,
                            title="MEMORY AND STORAGE STATS",
                            title_align="left",
                            border_style="#a96ebb"
                        )
                    )

                    layout["lowerleft"].update(
                        Panel(
                            chat_data,
                            title="CHAT BOT (ASK QUESTIONS)",
                            title_align="left",
                            border_style="#6785b5"
                        )
                    )

                    layout["upper"].update(
                        Panel(
                            cpu_data,
                            title="CPU STATS",
                            title_align="left",
                            border_style="#91b461"
                        )
                    )

                    layout["middle"].update(
                        Panel(
                            gpuText,
                            title="GPU STATS",
                            title_align="left",
                            border_style="#b4a96d"
                        )
                    )

                    layout["lower"].update(
                        Panel(
                            proc_data,
                            title="BASIC PROCESSES",
                            title_align="left",
                            border_style="#b89f91"
                        )
                    )

                    layout["upperright"].update(
                        Panel(
                            net_data,
                            title="NETWORK STATS",
                            title_align="left",
                            border_style="#b05151"
                        )
                    )

                    layout["lowerright"].update(
                        Panel(
                            pkt_data,
                            title="PACKET FILTERING",
                            title_align="left",
                            border_style="#b67752"
                        )
                    )

                    time.sleep(1 / 24)

    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, oldSettings)

if __name__ == "__main__":
    main()
#endregion
