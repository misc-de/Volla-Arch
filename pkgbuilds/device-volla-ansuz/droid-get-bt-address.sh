#!/bin/sh
# Bluetooth address from NVRAM: MediaTek keeps it in the first six bytes of the BT NVRAM file,
# in display order (the WLAN MAC is the same plus one). The vendor HAL cannot read that file
# itself (libnvram also opens files of group system) and would make up a random address on
# every start, so hand it over as pre-set address before bluebinder initializes the HAL.
# bluebinder_post.sh calls this again for /var/lib/bluetooth/board-address.
set -eu
addr=$(od -An -tx1 -N6 /mnt/vendor/nvdata/APCFG/APRDEB/BT_Addr | tr -s ' \n' ':' | sed 's/^://; s/:$//')
setprop persist.service.bdroid.bdaddr "$addr"
mkdir -p /var/lib/bluetooth
echo "$addr" > /var/lib/bluetooth/board-address
