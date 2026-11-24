<p align="center">
  <img src="https://plinius.cais.de/images/2_plinius_phosh.png?v=2026112102" width="250" alt="Home screen: open apps above, favorites and every app below, on black with drifting dust">
  &nbsp;&nbsp;
  <img src="https://plinius.cais.de/images/5_plinius_quickmenue.png?v=2026112102" width="250" alt="Quick settings with Wi-Fi, Bluetooth, hotspot and VNC desktop">
</p>

<p align="center"><sub><b>VOLLA PHONE PLINIUS &amp; PLINIUS PLUS</b></sub></p>

<h1 align="center">A real Linux phone,<br><em>always up to date.</em></h1>

<p align="center">
  Arch Linux with Phosh on a current smartphone. The manufacturer's own drivers provide every
  function the phone needs, and Arch Linux ARM runs on top, so every upstream release reaches the
  phone right away. The desktop feeling on your smartphone.
</p>

<p align="center">
  <a href="https://plinius.cais.de/#get"><b>Get the image</b></a> &nbsp;·&nbsp;
  <a href="#screenshots"><b>See it running</b></a> &nbsp;·&nbsp;
  <a href="TECHNICAL.md"><b>Technical details</b></a> &nbsp;·&nbsp;
  <a href="https://plinius.cais.de/">plinius.cais.de</a>
</p>

<p align="center">
  <code>Arch Linux ARM, rolling</code> <code>Phosh 0.59</code> <code>Kernel 6.1 LTS</code>
  <code>Fully encrypted Linux installation</code> <code>Open source</code>
</p>

<sub><b>WHY PLINIUS</b></sub>

## Everything a desktop Linux has, in your pocket

No reduced port where half the hardware stays dark. The phone keeps its vendor drivers and gets a
normal, writable Arch system on top.

<table>
<tr>
<td width="33%" valign="top">

### 🔄 Always the latest software

Base system and desktop come unchanged from Arch Linux ARM. Our own packages arrive from a signed
repository, in a stable and a beta channel.

</td>
<td width="33%" valign="top">

### 📱 The hardware works

GPU with OpenGL ES 3.2, Vulkan 1.3 and WebGL, display at 60/90/120&nbsp;Hz, all four cameras, fingerprint under
the display, face unlock on the phone's own NPU, entirely local, calls with VoLTE, SMS, cell
broadcast warnings, NFC, eSIM, GPS and the sensors.

</td>
<td width="33%" valign="top">

### 🔒 Private by design

The data partition is encrypted and asks for its password at boot. A DNS filter blocks ads and
trackers, with its own rules for each app. Access Control limits what each app may reach.

</td>
</tr>
<tr>
<td valign="top">

### 🎙️ Speech to text, offline

Hold the microphone key on the on-screen keyboard and speak. Recognition runs on the phone. Nothing
goes to a cloud.

</td>
<td valign="top">

### 🖥️ Your phone is a desktop

Connect a PC over USB or Wi-Fi, or send the desktop to a TV, and the phone becomes a desktop.
Running apps switch seamlessly between the phone and the desktop view with a tap, without restarting.

</td>
<td valign="top">

### ↩️ Updates you can undo

A self-test runs after every boot and offers a way back from the last update. A new kernel first
starts as a trial, and the previous one stays as a fallback.

</td>
</tr>
</table>

<sub><b>SCREENSHOTS</b></sub>

## Screenshots

Phosh, made for this phone: GNOME apps that adapt to the small screen, a settings app with pages
for the phone, and a quick menu with everything a phone needs.

<table>
<tr>
<td align="center" width="25%"><img src="https://plinius.cais.de/images/2_plinius_phosh.png?v=2026112102" width="180" alt="Home screen: open apps above, favorites and every app below, on black with drifting dust"><br><sub>Home screen with open apps</sub></td>
<td align="center" width="25%"><img src="https://plinius.cais.de/images/5_plinius_quickmenue.png?v=2026112102" width="180" alt="Quick settings with Wi-Fi, Bluetooth, hotspot and VNC desktop"><br><sub>Quick settings</sub></td>
<td align="center" width="25%"><img src="https://plinius.cais.de/images/3_plinius_gcc.png?v=2026112102" width="180" alt="Settings app with phone pages"><br><sub>Settings</sub></td>
<td align="center" width="25%"><img src="https://plinius.cais.de/images/4_plinius_security.png?v=2026112102" width="180" alt="Device security checks"><br><sub>Device security at a glance</sub></td>
</tr>
<tr>
<td align="center"><img src="https://plinius.cais.de/images/7_plinius_dnsfilter.png?v=2026112102" width="180" alt="DNS filter with statistics"><br><sub>DNS filter / ad blocker</sub></td>
<td align="center"><img src="https://plinius.cais.de/images/9_plinius_apps.png?v=2026112102" width="180" alt="App store with categories"><br><sub>App store with Arch and Flathub</sub></td>
<td align="center"><img src="https://plinius.cais.de/images/10_plinius_battery.png?v=2026112102" width="180" alt="Battery page with charging limit"><br><sub>Battery care and consumption</sub></td>
<td align="center"><a href="https://plinius.cais.de/#screens">More on<br><b>plinius.cais.de</b> →</a></td>
</tr>
</table>

### 🖥️ Desktop on a PC or TV

Every app on the smartphone can be used as a desktop app: on a PC over VNC, by USB cable or Wi-Fi,
or on a TV, with the phone as touchpad and keyboard. An app menu and settings let the desktop and
the smartphone work on the same apps together.

<p align="center"><img src="https://plinius.cais.de/images/6_desktop.png" alt="The phone's desktop on a large screen"></p>

### 🕹️ DOS games

The DOS Installer sets up MS-DOS games from floppy disk images, CDs, archives or GOG installers.
Each game gets its own icon in the app overview and starts full screen in DOSBox or ScummVM. A stick
and buttons beside the picture send the keys the game wants.

<p align="center"><img src="https://plinius.cais.de/images/11_plinius_dos.png?v=2026112304" alt="DOOM II full screen in landscape, the touch stick and buttons beside the picture"></p>

<sub><b>INSTALLATION</b></sub>

## From Volla OS to Arch in three steps

The PC app **Plinius Boot** guides you through everything. It installs to a microSD card by
default, so the system you have now stays on the phone.

1. **Start Plinius Boot.** Start Plinius Boot on a Linux PC and connect the phone with a USB-C
   cable.
2. **First check, unlock if needed.** The app checks the phone and shows what is still missing. If
   the bootloader is locked, it guides you through unlocking step by step.
3. **Choose the image, set up, install.** Download the newest image right in the app or choose a
   file on the PC. Then pick the microSD card or the internal storage and the language, and the
   app installs.

> [!WARNING]
> **Unlocking the bootloader erases the phone.** Copy your photos, messages and files off the phone
> first.

<sub><b>HONEST STATUS</b></sub>

## Where the project stands

Plinius is the daily phone of its developer. It is an early project and not yet a finished system
for everyone. Here is what that means today.

<table>
<tr>
<th width="34%">✅ Works</th>
<th width="33%">🟡 Partly</th>
<th width="33%">⏳ Not yet</th>
</tr>
<tr>
<td valign="top">

- Calls over VoLTE, SMS, mobile data, hotspot
- GPU, display at 60/90/120&nbsp;Hz, night light, always-on display
- All four cameras, flashlight
- Fingerprint, PIN or pattern
- Wi-Fi, NFC, GPS, sensors
- Speaker, headphones, Bluetooth headsets, car kits
- Encryption, DNS filter, updates with a way back

</td>
<td valign="top">

- Face unlock without a liveness check
- eSIM: profiles in Settings, moving a contract to the phone not tested yet
- Echo cancellation in calls
- App security (Access Control), not finished yet

</td>
<td valign="top">

- Verified boot: the bootloader stays unlocked
- Wi-Fi calling
- Emergency calls not tested on a real network

</td>
</tr>
</table>

The full status is on the technical page under [State](TECHNICAL.md#state).

<sub><b>THE IMAGE</b></sub>

## Get Plinius

One image with everything a start needs: Arch Linux ARM with Phosh, the hardware layer and the
kernel, plus ready apps such as Firefox, Maps, Calendar, Camera, Phone and Messages, and speech
recognition. **The first public image is being prepared**; it will appear on
[plinius.cais.de](https://plinius.cais.de/#get), together with Plinius Boot, the PC app that
downloads and installs it.

You need:

- Volla Phone Plinius or Plinius Plus
- A PC with Linux and a USB cable
- A microSD card of 64&nbsp;GB or more, unless you install to the phone's internal storage

<sub><b>TECHNICAL DETAILS</b></sub>

## Under the hood

How the phone's hardware is reached and how to build an image yourself:
**[TECHNICAL.md](TECHNICAL.md)** has the key facts, the full status, a comparison with
other systems, the architecture, building, and the third-party projects and licenses.

<p align="center"><sub>Plinius is an independent community project, not made by Volla.</sub></p>
