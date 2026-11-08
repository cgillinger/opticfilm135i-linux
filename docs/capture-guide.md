# Capturing the vendor software's USB traffic

**Everything in this guide is done at your own risk.** You will be
driving your scanner with the manufacturer's Windows software inside a
virtual machine, and recording the USB traffic on the Linux side. The
scanner's mechanics are driven by that software, not by this project,
and nobody here can see your setup or stop a movement for you. If you
are not comfortable with that, do not do it: the driver works without
your capture, and a capture is only worth making when you have a
specific question it can answer. The authors of this project take no
responsibility for damage to your scanner, your computer or your
data.

## When a capture is the right tool

The driver replays what the manufacturer's software sends, as data
tables (`of135i/tables.py`, `docs/protocol-notes.md`). When a scanner
behaves differently from the unit the tables came from, the only way to
learn what the manufacturer's software does differently on that unit is
to record it. Typical cases:

- another model in the same family (the OpticFilm 135 without infrared,
  issue #72): the register values for the lamp, the exposure and the
  calibration may differ, and a capture gives them directly;
- a mechanism the driver gets wrong on a unit (a load that misses, a
  position that is off) where guessing would mean more motor runs on an
  untested path.

A capture is not needed to report a problem. The `doctor` report, the
`diag.json` and `cal.npz` files the driver writes next to each frame,
and a log are what to send first (see `CONTRIBUTING.md`).

## What you need

- A Linux host with the scanner on its USB, root access, the `usbmon`
  kernel module (part of every mainstream kernel), `tcpdump` (or
  Wireshark) and, to check the file afterwards, `tshark`.
- A Windows virtual machine on that host with USB passthrough: VMware
  Workstation/Player, VirtualBox (with the extension pack for USB 2/3)
  or QEMU/KVM with `usb-host`. The manufacturer's driver and the
  QuickScan application installed in it (the traces in this repository
  came from QuickScan 6.0).
- Time, and your attention for the whole run: you stay at the scanner.

A capture made on a Windows machine directly, with USBPcap and
Wireshark, is also readable. The compile step in this repository
(`tools/compile_trace.py`) was written for usbmon captures and may need
adjusting for a USBPcap file; send it anyway and say which tool made it.

## Safety rules (read before you start)

**At your own risk, again.** These rules are what kept the one
development unit intact; they are not a guarantee.

1. **One program at a time.** While the virtual machine has the scanner,
   nothing on the Linux side may talk to it: no `of135i` command, no
   SANE frontend, no digiKam, no `scanimage`. Two drivers on one scanner
   is how a mechanism ends up in an unknown state.
2. **The virtual machine must not grab the scanner on its own.** VMware
   and VirtualBox can auto-attach a USB device whenever it re-enumerates
   (every power cycle). Turn that off, or you will find the Linux side
   locked out with "Resource busy" at the wrong moment.
3. **Stay at the scanner and listen.** Grinding or scraping means a
   stalled motor: switch the scanner off at the wall immediately, before
   anything else. Do not wait for the software, do not click Cancel first.
4. **Never interrupt a movement.** Do not close the application, detach
   the device or suspend the virtual machine while the holder is moving.
5. **After the capture, power-cycle the scanner** before any Linux-side
   use. The manufacturer's software leaves the mechanism in its own idle
   state; the Linux driver expects the state a power cycle gives (see
   `README.md`, "Power-cycling"). The driver refuses to start from a
   state it does not recognise, and that refusal is the right outcome.
6. **Close the application completely** before detaching the scanner
   from the virtual machine. QuickScan keeps running in the background
   after its window is closed; end it from Task Manager and check that
   it is gone.
7. **Do not run captures unattended or in loops.** Every scan is a motor
   pass. A script that scans repeatedly on an untested model is exactly
   the risk this guide exists to avoid.

## Step by step

### 1. Prepare the virtual machine (once)

Install the manufacturer's driver and QuickScan in the virtual machine.
Attach the scanner once (VM menu → Removable Devices / USB → the Plustek
device) and check in Device Manager that it appears (VID 07B3). Then
detach it, and turn off automatic connection for that device.

### 2. Prepare the host

```bash
sudo modprobe usbmon
lsusb | grep 07b3            # Bus 001 Device 005: ID 07b3:...  → bus 1 → usbmon1
```

The capture interface is `usbmonN` where N is the bus number from
`lsusb` (`usbmon1` for bus 001). Choose an **absolute** path for the
capture file; `~` does not expand inside `sudo`.

### 3. Start from a known state

Power-cycle the scanner. Put the film holder in the slot the way the
Linux driver's README describes for loading (loose in the slot, not
pushed in). Do not start QuickScan yet.

### 4. Start the capture

In its own terminal, left running:

```bash
sudo tcpdump -i usbmon1 -s 0 -w /home/you/captures/135-quickscan-first-run.pcap
```

`-s 0` records the whole of every transfer, including the image data,
which is what makes the register values and the data layout readable
together. See "Size and privacy" below for the smaller variant.

### 5. Run the manufacturer's software

Attach the scanner to the virtual machine, then start QuickScan and
wait until its start-up movements are finished (it moves the holder: this
is the manufacturer's own cold start). Then, in this order, one thing at
a time, noting the clock time of each step:

1. Load the film (insert the holder when the application asks).
2. One scan: 3600 dpi, colour, one frame, with the application's default
   settings. If the application offers infrared dust removal, make one
   scan with it off and, if your scanner has infrared, one with it on.
3. Eject.
4. Close QuickScan completely (Task Manager), detach the scanner from
   the virtual machine.

Stop the capture with Ctrl-C in the tcpdump terminal. Power-cycle the
scanner.

Several short captures, one per action, are easier to read than one
long one. If you can, repeat steps 3–5 with a new file for each of:
application start only; load only; one scan only; eject only.

### 6. Write down what happened

A capture without its timeline is hard to use. For each file:

- scanner model and product id (`lsusb`), QuickScan version;
- which holder, what was in it (empty, a strip, a slide; which frame);
- the settings used for the scan;
- the clock time of each action, and anything you heard or saw that
  seemed off.

### 7. Check the file (optional)

```bash
tshark -r /home/you/captures/135-quickscan-first-run.pcap -q -z io,phs | head
.venv/bin/python tools/compile_trace.py /home/you/captures/135-quickscan-first-run.pcap /home/you/captures/out
```

The first command shows that the file holds USB traffic; the second
compiles it into the trace format the driver's tables are built from
and tells you whether it is readable as-is.

## Size and privacy

A 3600 dpi frame is about 120 MB of image data, and the capture holds
it in full, so a file with one scan is 150–250 MB. Two ways to keep it
manageable:

- scan an empty holder, or a frame you do not mind other people seeing,
  and compress the file (`zstd -19` or `xz -9`; the control traffic
  compresses very well);
- or capture with `-s 256` instead of `-s 0`: every transfer is cut to
  its first 256 bytes, which keeps all the commands, registers and status
  reads and drops the picture content. The file is then a few megabytes.
  Make the first capture with `-s 0` if you can; the full data settles
  questions about the image layout that a truncated one cannot.

GitHub attachments are limited to 25 MB and to certain file types, so a
larger capture goes through a file-transfer link posted in the issue.

## What happens with the capture

The maintainer compiles it (`tools/compile_trace.py`) and compares the
register writes against the traces this driver was built from
(`docs/protocol-notes.md`, pass 17 describes the method on the dpi
captures). The outcome is a list of registers that differ, which says
whether the other model is a table difference (a variant in the same
driver, tested by its owners) or something larger. The capture itself
is not added to the repository: it contains the manufacturer's command
stream and possibly your picture, and the repository holds only derived
constants and our own code.
