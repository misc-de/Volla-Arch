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

<sub><b>FAQ</b></sub>

## Questions and answers

<details>
<summary><b>Are there enough apps?</b></summary>

By now there is a good choice of useful apps made for phones, yet next to the established platforms
their number is still small. **Settings › Apps › Explore** shows for each app whether it fits the
screen. Flathub adds several hundred more apps; switch it on under **Settings › Apps › Settings**.

Many service providers offer no app for Linux at all, and that can get in the way. Their website
often does the same job: under **Settings › Apps › Web Apps** any website becomes an app with its
own icon, without the browser's bars and with a profile of its own. That closes much of the gap.

</details>

<details>
<summary><b>Which messengers work?</b></summary>

Messengers such as Matrix and Delta Chat come as native apps made for phones, for example Fractal
for Matrix. They run on Plinius alone, with an account of their own.

WhatsApp, Telegram and Signal work as an additional device of an existing account, the way they also
run on a computer: you link Plinius to your account, usually by scanning a QR code with the phone
the account is on. Depending on the service this works through an app from the catalog or from
Flathub, or as a web app under **Settings › Apps › Web Apps**.

</details>

<details>
<summary><b>Can I go back to Volla OS or Ubuntu Touch?</b></summary>

Yes, at any time. By default Arch Linux goes onto a microSD card and the system inside the phone
stays as it is: without the card, or picked in the boot menu, the phone starts Volla OS or Ubuntu
Touch as before.

If Arch Linux has replaced Volla OS inside the phone, Plinius Boot first saved a backup of Volla OS
on your computer, and under Advanced it writes that backup back. Volla's own installation packages
for Volla OS and the UBports Installer for Ubuntu Touch work as well.

</details>

<details>
<summary><b>How do updates work?</b></summary>

The phone looks for updates every 4 hours and reports them with a notification; you install them
under **Settings › System › Updates**. The base system and the desktop come unchanged from Arch
Linux ARM, but only in versions we have tested on the phone; our own packages come signed from our
repository. If you want new versions earlier, switch on **Settings › Apps › Settings › Beta
Updates**.

After every start a self-test checks whether everything an update can break still works. If a check
fails after an update, **Settings › System › Self-Test** offers to take the last update back. A new
kernel first starts as a trial, and the previous one stays as a fallback.

</details>

<details>
<summary><b>Why does the GPS take a while to find my position?</b></summary>

Arch Linux finds your position by satellite alone; it never asks a server about the Wi-Fi networks
and cell towers around you. For a position the GPS needs at least four satellites and must know
their orbits. Without that knowledge it reads the orbits from the satellites' own signals, which
takes up to about a minute.

MediaTek, the maker of the phone's chip, offers predicted orbits for download. With them the GPS
finds the position within seconds. Each download shows MediaTek your IP address, so it is off on a
new phone. Switch it on under **Settings › Privacy › Location › Faster GPS Fix**: it loads about
80&nbsp;KB every 6 hours and sends no data.

</details>

<details>
<summary><b>What works in the mobile network?</b></summary>

Calls over VoLTE, SMS, mobile data, the hotspot and cell broadcast warnings work, and eSIM profiles
can be managed in Settings. Wi-Fi calling (VoWiFi) does not work yet.

MMS is built in but untested: German networks no longer offer MMS, so it could not be tried here.

</details>

<details>
<summary><b>How long does the battery last?</b></summary>

In standby with the display off the phone sleeps most of the time and uses about 2 to 3&nbsp;% of
the battery per hour. How long a charge lasts in active use has not been measured systematically
yet; the processor takes only a small share of the energy, most of it goes to the display and the
radios.

**Settings › Battery** shows how much energy each app used and how healthy the battery is, and
offers a charging limit that spares the battery when the phone is often on the charger. Wireless
charging with Qi works too.

</details>

<details>
<summary><b>Why does my phone list security vulnerabilities?</b></summary>

Most phones only show the date of their last security update. Arch Linux names every known
vulnerability in the installed software and in the kernel, under **Settings › Security › Known
Vulnerabilities**, each with a link to its description. The list is updated every day, and each
vulnerability we have looked into carries our assessment, such as “Fixed here” or “Mitigated”.

Once a vulnerability becomes known, a fix is usually under way and soon reaches the phone with the
next updates. Until then you know what is open and can deal with it deliberately. This openness
takes you seriously: it is your phone, so you get to know its weak spots too.

</details>

<details>
<summary><b>Which connections does the phone make by itself?</b></summary>

Every 4 hours it looks for updates (our repository on plinius.cais.de and the servers of Arch Linux
ARM), several times a day it loads the list of known vulnerabilities (Arch Linux security tracker
and plinius.cais.de), once a week the keys that sign Arch Linux's packages, and it sets its clock
from the Arch Linux time servers of pool.ntp.org. NetworkManager regularly asks ping.archlinux.org
whether the internet is reachable; that is how it recognises Wi-Fi networks with a login page. The
DNS filter loads its block lists from their authors.

Only when you switch them on: the GPS orbit data from MediaTek, Flathub, the Widevine module for
streaming services and contributions to beaconDB. The system itself sends no usage data and needs no
account.

</details>

<details>
<summary><b>The bootloader stays unlocked. Is that a risk?</b></summary>

An unlocked bootloader starts any system without checking who made it. Arch Linux needs that: the
boot menu, the rescue system and Arch Linux itself are not signed by the phone's maker. It applies
to every system on the phone, Volla OS inside it included. Your data stays protected all the same:
the data partition with the system on it is fully encrypted, and whoever takes the phone reads
nothing without the encryption password.

What remains is a calculable scenario: someone who has the phone in hand unnoticed could swap its
start files to capture the password the next time you type it. Arch Linux cannot prevent that, but
it checks its start files at every start and reports any change it did not make itself. If it
reports a manipulation you did not cause, treat the encryption password as known.

</details>

<details>
<summary><b>What happens if I forget the encryption password?</b></summary>

Then nobody can open the data any more: not you, not us, not Plinius Boot. There is no master key
and no back door; that is exactly what makes the encryption safe. Only a new installation helps, and
everything on the phone is lost.

So write the password down and keep it in a safe place away from the phone. The phone asks for it at
every start; it is not the PIN of the lock screen, the two are different on purpose.

</details>

<details>
<summary><b>Why are there Android drivers inside?</b></summary>

The phone's chip comes from MediaTek, and its drivers for graphics, camera, modem, sound and sensors
exist only for Android, without source code. Plain Linux (mainline) does not reach this chip yet:
there would be no graphics, no Wi-Fi, no modem and no sound.

Halium solves this: the Android drivers run in a small container of their own, and Linux uses them
through libhybris and libgbinder. Everything you see and use above that is ordinary Arch Linux.
Ubuntu Touch and Droidian work the same way.

</details>

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
