// plinius-widevine: the Widevine CDM that plinius-widevine-update fetched from Google, found
// through MOZ_GMP_PATH (/var/lib/plinius-widevine/gmp-widevinecdm/system-installed).
// Mozilla offers no Widevine for Linux arm64, so Firefox must not look for one itself.
// Settings > General > "Play DRM-controlled content" still turns DRM off (media.eme.enabled).
pref("media.gmp-widevinecdm.version", "system-installed");
pref("media.gmp-widevinecdm.visible", true);
pref("media.gmp-widevinecdm.enabled", true);
pref("media.gmp-widevinecdm.autoupdate", false);
pref("media.eme.enabled", true);
pref("media.eme.encrypted-media-encryption-scheme.enabled", true);
