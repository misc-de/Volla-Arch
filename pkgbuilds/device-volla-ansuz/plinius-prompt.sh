# Plinius: bash prompt "user@host:path$ " as on the build host, the user name in bold and in the
# accent color of the desktop; host and full path stay in the terminal's own color. Sourced by
# /etc/bash.bashrc for interactive shells (device-volla-ansuz.install adds that line).
#
# Only Arch's default prompt '[\u@\h \W]\$ ' is replaced, so a prompt of one's own in ~/.bashrc
# stays. ~/.bashrc of Arch's skeleton sets that default again after this file has run, so the
# prompt is swapped from PROMPT_COMMAND before the first prompt instead of here.
#
# The color is read once per shell: a new accent color shows in new terminals.

# Accent color as "#rrggbb": org.plinius.appearance (a color of one's own) first, else the named
# GNOME color of org.gnome.desktop.interface, as Phosh does (phosh-plinius 0015).
__plinius_prompt_accent() {
	local home cfg hex name
	command -v gsettings >/dev/null || return 1
	cfg=${XDG_CONFIG_HOME:-$HOME/.config}
	# root (e.g. over SSH) has no desktop of its own: take the settings of the phone's user, the
	# first account with a login UID and a home under /home. Reading needs no session bus.
	if [[ $EUID -eq 0 && ! -e $cfg/dconf/user ]]; then
		home=$(getent passwd | awk -F: '$3 >= 1000 && $3 < 60000 && $6 ~ "^/home/" { print $6; exit }')
		[[ -n $home ]] && cfg=$home/.config
	fi
	hex=$(XDG_CONFIG_HOME=$cfg gsettings get org.plinius.appearance accent-color 2>/dev/null)
	hex=${hex//\'/}
	if [[ ! $hex =~ ^#[0-9A-Fa-f]{6}$ ]]; then
		name=$(XDG_CONFIG_HOME=$cfg gsettings get org.gnome.desktop.interface accent-color 2>/dev/null)
		# GNOME's accent colors as libadwaita draws them (also in cc-plinius-accent.c)
		case ${name//\'/} in
			teal) hex='#2190a4' ;; green) hex='#3a944a' ;; yellow) hex='#c88800' ;;
			orange) hex='#ed5b00' ;; red) hex='#e62d42' ;; pink) hex='#d56199' ;;
			purple) hex='#9141ac' ;; slate) hex='#6f8396' ;; *) hex='#3584e4' ;;
		esac
	fi
	printf '%d;%d;%d' "0x${hex:1:2}" "0x${hex:3:2}" "0x${hex:5:2}"
}

__plinius_prompt_rgb=
[[ $TERM != dumb ]] && __plinius_prompt_rgb=$(__plinius_prompt_accent)

__plinius_prompt() {
	# \u@\h:\w\$ with the user name bold and colored (24-bit color, \[ \] keep the line length
	# right); without a color (dumb terminal, no settings) the same layout uncolored
	[[ $PS1 == '[\u@\h \W]\$ ' ]] || return 0
	if [[ -n $__plinius_prompt_rgb ]]; then
		PS1='\[\e[1;38;2;'"$__plinius_prompt_rgb"'m\]\u\[\e[0m\]@\h:\w\$ '
	else
		PS1='\u@\h:\w\$ '
	fi
}
PROMPT_COMMAND+=(__plinius_prompt)
