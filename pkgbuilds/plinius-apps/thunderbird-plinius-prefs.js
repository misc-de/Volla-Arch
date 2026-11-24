// Defaults for Thunderbird on the Volla Phone (Plinius).
// Plain default (pref), not locked: it can still be changed in the config editor.

// Hardware video decoding: Thunderbird probes every /dev/video* with its own gfxtest
// process at start and waits for each. The phone has ~170 V4L2 nodes (MediaTek camera
// and codec): 15 s instead of 2 s until the window, with any profile. Nothing is lost,
// see plinius-prefs.js of mobile-config-firefox for the same setting in Firefox.
pref("media.hardware-video-decoding.enabled", false);
