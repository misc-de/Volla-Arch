# plinius-widevine: where Firefox finds the Widevine CDM; see /usr/lib/environment.d/50-plinius-widevine.conf
case ":${MOZ_GMP_PATH}:" in
	*:/var/lib/plinius-widevine/gmp-widevinecdm/system-installed:*) ;;
	*) export MOZ_GMP_PATH="${MOZ_GMP_PATH:+$MOZ_GMP_PATH:}/var/lib/plinius-widevine/gmp-widevinecdm/system-installed" ;;
esac
