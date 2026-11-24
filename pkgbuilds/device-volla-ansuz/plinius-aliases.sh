# Plinius: default aliases for interactive bash shells, for every user including root. Sourced by
# /etc/bash.bashrc (device-volla-ansuz.install adds that line), which runs before ~/.bashrc, so
# an alias of one's own there wins and "unalias" removes one of these.
alias ls='ls --color=auto'
alias ll='ls -l --color=auto'
alias l='ls -la --color=auto'
alias ..='cd ..'
alias cls='clear'
alias md='mkdir'
alias rm='rm -i'
alias mv='mv -i'
# Only where vim is installed: otherwise "vi" would turn into a missing command
if command -v vim >/dev/null; then
	alias vi='vim'
fi
