// Defaults for Firefox on the Volla Phone (Plinius): start time, security, privacy.
// Plain defaults (pref), not locked: everything can still be changed in Settings or
// about:config. Telemetry, studies, Pocket and uBlock Origin are policies
// (policies.json of mobile-config-firefox).

// --- Start time ---
// Hardware video decoding: Firefox probes every /dev/video* with its own gfxtest
// process at start and waits for each. The phone has ~170 V4L2 nodes (MediaTek
// camera and codec): 15.9 s instead of 2.4 s until the window. Nothing is lost:
// VA-API has no render node here and the MediaTek decoder is stateless, which
// Firefox' V4L2-M2M path cannot drive.
pref("media.hardware-video-decoding.enabled", false);
// Fewer writes to the slow SD card: session state every 60 s instead of 15 s
pref("browser.sessionstore.interval", 60000);
// Local AI features (models, chatbot sidebar): downloads and memory, no use here
pref("browser.ml.enable", false);
pref("browser.ml.chat.enabled", false);
pref("browser.shell.checkDefaultBrowser", false);

// --- Add-ons ---
// Swipe gestures (own add-on, forced by policy) also on Mozilla's quarantined domains
// (extensions.quarantinedDomains.list, a few banking sites): the switch "Run on sites with
// restrictions" in about:addons. A new profile had it off, so the add-on looked inactive.
// uBlock Origin is a recommended add-on, which the quarantine never applies to.
pref("extensions.quarantineIgnoredByUser.swipe-gestures@de.cais", true);

// --- Security ---
// HTTPS-Only: plain HTTP only after an explicit click per site
pref("dom.security.https_only_mode", true);
// Show internationalised domains as punycode (lookalike phishing domains)
pref("network.IDN_show_punycode", true);
// No JavaScript inside PDFs
pref("pdfjs.enableScripting", false);
// Ask before opening downloaded files of a new type
pref("browser.download.always_ask_before_handling_new_types", true);

// --- Privacy ---
// Enhanced Tracking Protection "Strict" is a policy (EnableTrackingProtection): the
// category pref alone is only a label, Firefox reset it to "standard" on start

pref("privacy.globalprivacycontrol.enabled", true);
// No connections to sites before they are opened (prefetch, predictor, speculative connects)
pref("network.prefetch-next", false);
pref("network.dns.disablePrefetch", true);
pref("network.predictor.enabled", false);
pref("network.http.speculative-parallel-limit", 0);
pref("browser.urlbar.speculativeConnect.enabled", false);
pref("browser.places.speculativeConnect.enabled", false);
// WebRTC: only the default route's address, no local IPs to websites
pref("media.peerconnection.ice.default_address_only", true);
// Safe Browsing stays on, but download metadata is not sent to Google
pref("browser.safebrowsing.downloads.remote.enabled", false);
// No sponsored content or Firefox Suggest in the address bar and on the new tab page
pref("browser.urlbar.suggest.quicksuggest.sponsored", false);
pref("browser.urlbar.suggest.quicksuggest.nonsponsored", false);
pref("browser.newtabpage.activity-stream.showSponsored", false);
pref("browser.newtabpage.activity-stream.showSponsoredTopSites", false);
pref("datareporting.healthreport.uploadEnabled", false);
