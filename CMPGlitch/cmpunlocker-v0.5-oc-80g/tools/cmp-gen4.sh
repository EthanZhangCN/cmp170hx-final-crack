#!/usr/bin/env bash
# CMP Gen4 staged link training (Gen1 -> Gen2 -> Gen3 -> Gen4) via RM control.
# Same pattern as cmp-gen3.sh: boot stays Gen2, this steps the link up to Gen4.
# Single pass, no indefinite retries. Judge success by LnkSta (16 GT/s).
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Root privileges required'; exit 1; }
BDF=${1:?Usage: sudo bash cmp-gen4.sh 0000:01:00.0}
[[ $BDF =~ ^0000:[0-9a-fA-F]{2}:00\.0$ ]] || {
  echo 'This tool supports only 0000:xx:00.0'; exit 1;
}
BDF=${BDF,,}
D=/sys/bus/pci/devices/$BDF
DEV=$(cat "$D/device")
[[ $(cat "$D/vendor") == 0x10de && ( $DEV == 0x20c2 || $DEV == 0x2082 ) ]] || {
  echo 'Device is not a CMP 170HX (10de:20c2 / 2082)'; exit 1;
}
PARENT=$(basename "$(dirname "$(readlink -f "$D")")")
[[ $PARENT =~ ^[[:xdigit:]]{4}:[[:xdigit:]]{2}:[[:xdigit:]]{2}\.[0-7]$ ]] || exit 1
[[ $(cat "/sys/bus/pci/devices/$PARENT/class") == 0x0604* ]] || {
  echo 'Upstream device is not a PCI bridge'; exit 1;
}
MINOR=$(awk -F: '/Device Minor/ {gsub(/[[:space:]]/, "", $2); print $2}' "/proc/driver/nvidia/gpus/$BDF/information")
[[ $MINOR =~ ^[0-9]+$ ]] || { echo 'Device Minor not found'; exit 1; }
BUS=${BDF:5:2}
RM=/usr/local/sbin/cmp-pcie-speed
[[ -x $RM ]] || { echo "Missing $RM (build tools/set_pcie_speed.c)"; exit 1; }
read_status() {
  local s
  s=$(setpci -s "$1" CAP_EXP+12.w)
  [[ $s =~ ^[[:xdigit:]]{4}$ && ${s,,} != ffff ]] || return 1
  printf '%s' "$s"
}
BEFORE=$(read_status "$BDF")
WIDTH=$(( (16#$BEFORE >> 4) & 63 ))
(( WIDTH > 0 )) || { echo 'Invalid link width'; exit 1; }
echo "GPU=$BDF upstream=$PARENT minor=$MINOR before=$BEFORE"
"$RM" 0 "$MINOR" "$BUS"
if (( (16#$BEFORE & 15) != 4 )); then
  "$RM" 1 "$MINOR" "$BUS"; sleep 1
  "$RM" 2 "$MINOR" "$BUS"; sleep 1
  "$RM" 3 "$MINOR" "$BUS"; sleep 1
  setpci -s "$PARENT" CAP_EXP+30.w=0004:000f
  setpci -s "$BDF" CAP_EXP+30.w=0004:000f
  "$RM" 4 "$MINOR" "$BUS"; sleep 1
fi
AFTER=$(read_status "$BDF")
UP=$(read_status "$PARENT")
GEN=$((16#$AFTER & 15))
NEW_WIDTH=$(( (16#$AFTER >> 4) & 63 ))
echo "after=$AFTER upstream=$UP => Gen$GEN x$NEW_WIDTH"
(( GEN == 4 && (16#$UP & 15) == 4 && NEW_WIDTH >= WIDTH )) || {
  echo 'FAILED: Gen4 was not established, or link width decreased'; exit 1;
}
lspci -s "$BDF" -vv | grep -E 'LnkCap:|LnkCtl2:|LnkSta:'
echo 'Gen4 link established; load and reboot validation are still required.'
