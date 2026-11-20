#!/usr/bin/env bash
#
# Deploys the Zscaler NSS VM with one command and reports the progress of the deployment.
# Linux and macOS. The Makefile calls this script; scripts/deploy.ps1 is the same for Windows.
#
#   scripts/deploy.sh preview   Shows what a deployment would change. Changes nothing.
#   scripts/deploy.sh deploy    Deploys main.sub.bicep and reports progress until it ends.
#   scripts/deploy.sh watch     Reports the progress of a deployment that was started earlier.
#   scripts/deploy.sh outputs   Prints the outputs of the last deployment.
#   scripts/deploy.sh destroy   Deletes what the deployment created in Azure, and the NSS
#                               server object it registered in ZIA. Shows what is deleted
#                               and deletes nothing before that is confirmed.
#
# Without RESOURCE_GROUP, preview and deploy ask for the values in the terminal, and offer
# the answers of the last run again. watch, outputs and destroy then use the resource group
# of those answers.
#
# Everything is done with the Azure CLI, with two exceptions: for the question about the
# SSH rule, a public service is asked for the IP address this computer has on the internet,
# and destroy deletes the NSS server object with scripts/delete-nss-server.py, which needs
# Python 3.10 or later.
#
# Settings, read from the environment:
#   RESOURCE_GROUP   Resource group of the NSS VM. Used as it is when it exists, created
#                    in LOCATION when it does not. When it is set, nothing is asked.
#   LOCATION         Azure region. Required for a new resource group. Defaults to the
#                    location of an existing resource group.
#   PARAMETER_FILE   Parameter file for main.sub.bicep. Default: main.sub.bicepparam.
#   PARAMETERS       Parameter values that replace those of the parameter file, as
#                    name=value pairs separated by spaces. A value cannot contain a space.
#   DEPLOYMENT_NAME  Name of the deployment record. Default: nss-<RESOURCE_GROUP>.
#   POLL_SECONDS     Seconds between two progress checks. Default: 20.
#   ASSUME_YES       Set to 1 to deploy without the confirmation question.
#   ANSWERS_FILE     File the answers are saved in. Default: .nss-deploy-answers in the
#                    repository. Secrets are never saved.
#   PUBLIC_IP_URL    Web addresses that answer with the public IP address of the caller as
#                    text, separated by spaces. The first answer is offered for the SSH
#                    rule when the values are asked. Default: https://api.ipify.org and
#                    https://checkip.amazonaws.com. Set to none to ask no service.
#   NO_COLOR         Set to any value for a progress bar without colours.
#
# Settings of destroy only:
#   CONFIRM_DESTROY  The name of the resource group, to destroy without the confirmation
#                    question. ASSUME_YES does not apply to destroy.
#   KEEP_NSS_SERVER  Set to 1 to leave the NSS server object in ZIA.
#   KEEP_RESOURCE_GROUP  Set to 1 to keep the resource group and delete only the resources
#                    of the deployment in it. Set to 0 to delete a resource group that
#                    holds nothing else, without the question that is asked when the
#                    deployment did not create it. One that holds anything else is kept.
#   NSS_SERVER_ID    ID of the NSS server object to delete in ZIA. Default: the ID the
#                    deployment recorded.
#   PYTHON           Python 3.10 or later that runs scripts/delete-nss-server.py. Default:
#                    python3. When it has no zscaler-sdk-python, that package is installed
#                    into a temporary virtual environment that is removed afterwards.
#
# Secrets are never arguments of this script. The parameter file reads them from the
# environment (NSS_SAS_TOKEN, NSS_VM_PASSWORD, ZSCALER_*, ZIA_*). This script only reports
# which of them are set and never prints a value. One that is not set is asked for without
# showing it when the values are asked, and is kept for this run only.
#
# Written for bash 3.2, the version macOS ships.

set -euo pipefail

REPOSITORY="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

ACTION="${1:-}"
RESOURCE_GROUP="${RESOURCE_GROUP:-}"
LOCATION="${LOCATION:-}"
PARAMETER_FILE="${PARAMETER_FILE:-}"
PARAMETERS="${PARAMETERS:-}"
DEPLOYMENT_NAME="${DEPLOYMENT_NAME:-}"
POLL_SECONDS="${POLL_SECONDS:-}"
ASSUME_YES="${ASSUME_YES:-}"
ANSWERS_FILE="${ANSWERS_FILE:-}"
CONFIRM_DESTROY="${CONFIRM_DESTROY:-}"
KEEP_NSS_SERVER="${KEEP_NSS_SERVER:-}"
KEEP_RESOURCE_GROUP="${KEEP_RESOURCE_GROUP:-}"
PYTHON="${PYTHON:-}"

[ -n "$PARAMETER_FILE" ] || PARAMETER_FILE="$REPOSITORY/main.sub.bicepparam"
[ -n "$POLL_SECONDS" ] || POLL_SECONDS=20
[ -n "$ANSWERS_FILE" ] || ANSWERS_FILE="$REPOSITORY/.nss-deploy-answers"
PUBLIC_IP_URL="${PUBLIC_IP_URL:-https://api.ipify.org https://checkip.amazonaws.com}"

SECRET_VARIABLES="NSS_SAS_TOKEN NSS_VM_PASSWORD ZSCALER_CLIENT_ID ZSCALER_CLIENT_SECRET ZSCALER_PRIVATE_KEY ZSCALER_VANITY_DOMAIN ZSCALER_CLOUD ZIA_USERNAME ZIA_PASSWORD ZIA_API_KEY ZIA_CLOUD"

OPERATIONS_QUERY="[?properties.targetResource].[properties.targetResource.resourceType, properties.targetResource.resourceName, properties.provisioningState, properties.targetResource.resourceGroup]"
FAILURES_QUERY="[?properties.provisioningState=='Failed' && properties.targetResource.resourceType!='Microsoft.Resources/deployments'].[properties.targetResource.resourceName, properties.statusMessage.error.code]"

# What destroy reads. An empty value would be lost in a line of values separated by tabs,
# so every value that can be empty is read as "-".
RECORD_QUERY="[[properties.provisioningState, properties.parameters.existingResourceGroupName.value || '-', properties.parameters.newResourceGroupName.value || '-', to_string(properties.parameters.installNssCertificate.value), properties.parameters.nssServerName.value || '-', properties.outputs.nssServerId.value || '-']]"
# Only what the deployment created: an operation that reads belongs to a resource the
# deployment uses and did not create, such as an existing storage account.
CREATED_QUERY="[?properties.provisioningOperation=='Create' && properties.targetResource.id].[properties.targetResource.resourceType, properties.targetResource.id]"
SCRIPTS_QUERY="[].[id, status.storageAccountId || '-', status.containerInstanceId || '-', outputs.nssServerId || '-']"

# The release of zscaler-sdk-python the certificate step is tested with, as it is pinned in
# scripts/install-nss-certificate.sh.
SDK_REQUIREMENT="zscaler-sdk-python==1.11.5"

# The copy script writes the progress of the VHD copy to its log in this form.
PROGRESS_PATTERN='^VHD copy (Pending|Success): ([0-9][0-9,.]*) of ([0-9][0-9,.]*) bytes copied\.$'

WORK=""
TARGET_GROUP=""
WATCHING=""
SUBSCRIPTION=""
GROUP_MODE=""
GROUP_LOCATION=""
GROUP_READ=""

# The questions. Everything that is asked is kept as name=value lines: ANSWERS holds the
# values of this run, SAVED those of the answers file.
QUESTIONS=""    # yes: the values come from questions and from the answers file
CAN_ASK=""      # yes: this is a terminal, so a question can be asked
ACCEPT_SAVED="" # yes: a saved answer that is still valid is taken without asking
ASKING=""       # yes: the questions are being asked
ASKED=""        # yes: at least one question was asked
API_KIND=""     # oneapi or legacy: the Zscaler API the credentials are asked for
# What a run that stops while it asks has not done.
NOTHING_DONE="Nothing was deployed."
# Destroy: what to say when Ctrl+C stops it after the confirmation.
STOP_NOTE=""
ANSWERS=""
SAVED=""
REGIONS=""
REPLY_TEXT=""
TTY_STATE=""
PUBLIC_ADDRESS=""
PUBLIC_ADDRESS_FROM=""

# The names that are saved, in the order of the questions. A name in capitals is a setting of
# this script or an environment variable; every other name is a parameter of main.sub.bicep.
ANSWER_NAMES="RESOURCE_GROUP LOCATION vmName virtualMachineSize vhdRegion sshSourceAddressPrefix vNetNewOrExisting NETWORK_DEFAULTS virtualNetworkName virtualNetworkResourceGroup subnetName subnetName2 vNetAddressRange subnetAddressRange subnetAddressRange2 stgNewOrExisting nameStorageAccount installNssCertificate nssServerName nssServerType certificateSubnetName certificateSubnetAddressRange ZSCALER_API ZSCALER_VANITY_DOMAIN ZSCALER_CLOUD ZIA_USERNAME ZIA_CLOUD"

# The values to choose from, as main.bicep allows them: "value" or "value|description".
VM_SIZE_OPTIONS=(
  "Standard_D2_v4|2 vCPUs, 8 GB"
  "Standard_D4_v4|4 vCPUs, 16 GB"
  "Standard_D8_v4|8 vCPUs, 32 GB"
  "Standard_D16_v4|16 vCPUs, 64 GB"
)
VHD_REGION_OPTIONS=(
  "znssprod|US Commercial"
  "znssprodeu|Europe"
  "znssprodau|Australia"
  "zsgovnsstexas|US Gov Texas"
  "zsgovnssarizona|US Gov Arizona"
  "zsgovnssiowa|US Gov Iowa"
  "zsgovnssvirginia|US Gov Virginia"
)
SERVER_TYPE_OPTIONS=(
  "SOFTWARE_AA_FLAG"
  "NSS_FOR_WEB"
  "NSS_FOR_FIREWALL"
  "VZEN"
  "VZEN_SME"
  "VZEN_SMLB"
  "PINNED_NSS"
  "MD5_CAPABLE"
  "ADP"
  "ZIRSVR"
  "NSS_FOR_ZPA"
)

# The progress bar of the VHD copy. On a terminal (LIVE) it is one line that is redrawn in
# place by a background loop (SPINNER_PID); every other line is printed above it.
ESC=$'\033'
LIVE=""
BAR_ACTIVE=""
BAR_OWNER=""
BAR_HEAD=""
BAR_TAIL=""
BAR_COMPLETE=""
SPINNER_PID=""
LAST_PROGRESS=""
TERMINAL_COLUMNS=80
COPIED=0
TOTAL=0

# Zscaler ASCII logo (colors match Zenith Live application_segment/main.py).
logo_rgb() {
  printf '\033[38;2;%d;%d;%dm' "$1" "$2" "$3"
}

logo_supports_truecolor() {
  [ -t 1 ] || return 1
  [ -n "${NO_COLOR:-}" ] && return 1
  case "${COLORTERM:-}" in
    truecolor | 24bit | TRUECOLOR | 24BIT) return 0 ;;
  esac
  case "${TERM:-}" in
    *256color* | *kitty* | *iterm*) return 0 ;;
  esac
  return 1
}

print_zscaler_logo() {
  local -a art=(
    '███████╗███████╗ ██████╗ █████╗ ██╗     ███████╗██████╗ '
    '╚══███╔╝██╔════╝██╔════╝██╔══██╗██║     ██╔════╝██╔══██╗'
    '  ███╔╝ ███████╗██║     ███████║██║     █████╗  ██████╔╝'
    ' ███╔╝  ╚════██║██║     ██╔══██║██║     ██╔══╝  ██╔══██╗'
    '███████╗███████║╚██████╗██║  ██║███████╗███████╗██║  ██║'
    '╚══════╝╚══════╝ ╚═════╝╚═╝  ╚═╝╚══════╝╚══════╝╚═╝  ╚═╝'
  )
  local width=0 line pad=2 inner i
  for line in "${art[@]}"; do
    [ "${#line}" -gt "$width" ] && width="${#line}"
  done
  inner=$((width + pad * 2))

  if ! logo_supports_truecolor; then
    echo
    printf '  +'
    for ((i = 0; i < inner; i++)); do printf '-'; done
    printf '+\n'
    for line in "${art[@]}"; do
      printf '  |'
      for ((i = 0; i < pad; i++)); do printf ' '; done
      printf '%s' "$line"
      for ((i = ${#line}; i < width + pad; i++)); do printf ' '; done
      printf '|\n'
    done
    printf '  +'
    for ((i = 0; i < inner; i++)); do printf '-'; done
    printf '+\n'
    echo
    return
  fi

  local reset=$'\033[0m'
  local border shadow_color
  border="$(logo_rgb 51 85 153)"
  shadow_color="$(logo_rgb 0 61 153)"
  local start_r=85 start_g=204 start_b=255
  local end_r=0 end_g=61 end_b=153
  local denom=$((width > 1 ? width - 1 : 1))
  local blank padded ch t rr gg bb last_r=-1 last_g=-1 last_b=-1 shadow

  blank=$(printf '%*s' "$width" '')
  shadow=$blank
  shadow=${shadow// /░}

  echo
  printf '  %s╭' "$border"
  for ((i = 0; i < inner; i++)); do printf '─'; done
  printf '╮%s\n' "$reset"
  printf '  %s│%s' "$border" "$reset"
  for ((i = 0; i < pad; i++)); do printf ' '; done
  printf '%s' "$blank"
  for ((i = 0; i < pad; i++)); do printf ' '; done
  printf '%s│%s\n' "$border" "$reset"
  for line in "${art[@]}"; do
    printf '  %s│%s' "$border" "$reset"
    for ((i = 0; i < pad; i++)); do printf ' '; done
    padded=$(printf '%-*s' "$width" "$line")
    last_r=-1 last_g=-1 last_b=-1
    for ((i = 0; i < width; i++)); do
      ch="${padded:$i:1}"
      if [ "$ch" = ' ' ]; then
        printf '%s' "$ch"
        continue
      fi
      t=$((i * 1000 / denom))
      rr=$((start_r + (end_r - start_r) * t / 1000))
      gg=$((start_g + (end_g - start_g) * t / 1000))
      bb=$((start_b + (end_b - start_b) * t / 1000))
      if [ "$rr" != "$last_r" ] || [ "$gg" != "$last_g" ] || [ "$bb" != "$last_b" ]; then
        printf '%s' "$(logo_rgb "$rr" "$gg" "$bb")"
        last_r=$rr
        last_g=$gg
        last_b=$bb
      fi
      printf '%s' "$ch"
    done
    printf '%s' "$reset"
    for ((i = 0; i < pad; i++)); do printf ' '; done
    printf '%s│%s\n' "$border" "$reset"
  done
  printf '  %s│%s' "$border" "$reset"
  for ((i = 0; i < pad; i++)); do printf ' '; done
  printf '%s%s%s' "$shadow_color" "$shadow" "$reset"
  for ((i = 0; i < pad; i++)); do printf ' '; done
  printf '%s│%s\n' "$border" "$reset"
  printf '  %s│%s' "$border" "$reset"
  for ((i = 0; i < pad; i++)); do printf ' '; done
  printf '%s' "$blank"
  for ((i = 0; i < pad; i++)); do printf ' '; done
  printf '%s│%s\n' "$border" "$reset"
  printf '  %s╰' "$border"
  for ((i = 0; i < inner; i++)); do printf '─'; done
  printf '╯%s\n' "$reset"
  echo
}

# Prints the comment at the top of this file, from its third line.

usage() {
  awk 'NR >= 3 { if ($0 !~ /^#/) exit; sub(/^# ?/, ""); print }' "${BASH_SOURCE[0]}"
}

die() {
  stop_spinner
  echo "ERROR: $*" >&2
  exit 1
}

say() {
  stop_spinner
  printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"
  start_spinner
}

# Decides how the progress of the VHD copy is shown. A bar that is redrawn needs a terminal.
setup_display() {
  local colors=0
  if [ -t 1 ] && [ "${TERM:-dumb}" != "dumb" ]; then
    LIVE="yes"
    [ -n "${NO_COLOR:-}" ] || colors="$(tput colors 2> /dev/null || echo 8)"
  fi
  case "$colors" in
    "" | *[!0-9]*) colors=0 ;;
  esac

  C_RESET="" C_EMPTY="" C_SPIN="" C_TEXT="" C_DONE=""
  GRADIENT=("")
  if [ "$colors" -ge 8 ]; then
    C_RESET="${ESC}[0m" C_EMPTY="${ESC}[2m" C_SPIN="${ESC}[35m" C_TEXT="${ESC}[1m" C_DONE="${ESC}[32m"
    GRADIENT=("36")
  fi
  if [ "$colors" -ge 256 ]; then
    C_EMPTY="${ESC}[38;5;240m" C_SPIN="${ESC}[38;5;213m" C_DONE="${ESC}[38;5;46m"
    GRADIENT=("38;5;33" "38;5;39" "38;5;45" "38;5;51" "38;5;50" "38;5;49" "38;5;48" "38;5;47" "38;5;46")
  fi

  case "${LC_ALL:-${LC_CTYPE:-${LANG:-}}}" in
    *[Uu][Tt][Ff]-8* | *[Uu][Tt][Ff]8*)
      FULL_CELL="█" EMPTY_CELL="░" DONE_MARK="✓"
      SPINNER=("⠋" "⠙" "⠹" "⠸" "⠼" "⠴" "⠦" "⠧" "⠇" "⠏")
      ;;
    *)
      FULL_CELL="#" EMPTY_CELL="-" DONE_MARK="*"
      SPINNER=("|" "/" "-" "\\")
      ;;
  esac
}

# Draws the progress bar with one position of the spinner.
draw_bar() {
  printf '\r%s%s%s%s' "$BAR_HEAD" "$C_SPIN${SPINNER[$(($1 % ${#SPINNER[@]}))]}$C_RESET" "$BAR_TAIL" "${ESC}[K"
}

# Draws the progress bar and redraws it ten times a second until stop_spinner ends that.
# The loop also ends when this script is gone.
start_spinner() {
  if [ -z "$BAR_ACTIVE" ] || [ -n "$SPINNER_PID" ]; then
    return 0
  fi
  draw_bar 0
  (
    frame=1
    while sleep 0.1 && kill -0 "$$" 2> /dev/null; do
      draw_bar "$frame"
      frame=$((frame + 1))
    done
  ) &
  SPINNER_PID=$!
}

# Ends the loop and clears the line of the bar, so that a line can be printed in its place.
stop_spinner() {
  [ -n "$SPINNER_PID" ] || return 0
  kill "$SPINNER_PID" 2> /dev/null || true
  wait "$SPINNER_PID" 2> /dev/null || true
  SPINNER_PID=""
  printf '\r%s' "${ESC}[K"
}

# Leaves the bar on the screen as a normal line and ends it.
end_bar() {
  [ -n "$BAR_ACTIVE" ] || return 0
  local mark=" "
  [ -z "$BAR_COMPLETE" ] || mark="$C_DONE$DONE_MARK$C_RESET"
  stop_spinner
  printf '%s%s%s\n' "$BAR_HEAD" "$mark" "$BAR_TAIL"
  BAR_ACTIVE=""
  BAR_OWNER=""
}

# Reads the bytes copied and the total from a progress line of the copy script.
parse_progress() {
  [[ "$1" =~ $PROGRESS_PATTERN ]] || return 1
  local copied="${BASH_REMATCH[2]//[^0-9]/}" total="${BASH_REMATCH[3]//[^0-9]/}"
  # More digits than this do not fit into the arithmetic of the shell.
  if [ -z "$copied" ] || [ -z "$total" ] || [ "${#copied}" -gt 16 ] || [ "${#total}" -gt 16 ]; then
    return 1
  fi
  COPIED=$((10#$copied))
  TOTAL=$((10#$total))
  [ "$TOTAL" -gt 0 ]
}

gibibytes() {
  local tenths=$(($1 * 10 / 1073741824))
  printf '%d.%d' "$((tenths / 10))" "$((tenths % 10))"
}

# Sets TERMINAL_COLUMNS to the width of the terminal this script writes to, or to 80 when
# it cannot be read. Not for a command substitution, whose output is not the terminal.
read_terminal_columns() {
  TERMINAL_COLUMNS=""
  # stty reports the size of the terminal that is its input.
  if stty size <&1 > "$WORK/terminal" 2> /dev/null; then
    read -r _ TERMINAL_COLUMNS < "$WORK/terminal" || true
  fi
  case "$TERMINAL_COLUMNS" in
    "" | *[!0-9]*) TERMINAL_COLUMNS=80 ;;
  esac
}

# Shows the progress that parse_progress read: on a terminal as a bar that is redrawn in
# place, otherwise as a line. The same progress is not shown twice.
show_progress() {
  local name="$1" depth="$2" percent text prefix width=0 filled cell=0 color cells=""
  percent=$((COPIED * 100 / TOTAL))
  [ "$percent" -le 100 ] || percent=100
  text="$(gibibytes "$COPIED") of $(gibibytes "$TOTAL") GiB"
  [ "$LAST_PROGRESS" != "$name|$percent|$text" ] || return 0
  LAST_PROGRESS="$name|$percent|$text"
  prefix="$(indent "$depth")  | VHD copy"

  if [ -n "$LIVE" ]; then
    read_terminal_columns
    # Besides the bar the line holds the time (11), the spinner (3), the percentage (8),
    # the prefix and the text. One column stays free so that the line never wraps.
    width=$((TERMINAL_COLUMNS - 23 - ${#prefix} - ${#text}))
    [ "$width" -le 30 ] || width=30
  fi
  if [ "$width" -lt 10 ]; then
    end_bar
    say "$prefix: $percent% ($text)"
    return 0
  fi

  stop_spinner
  filled=$((percent * width / 100))
  while [ "$cell" -lt "$filled" ]; do
    color="${GRADIENT[$((cell * ${#GRADIENT[@]} / width))]}"
    cells="$cells${color:+${ESC}[${color}m}$FULL_CELL"
    cell=$((cell + 1))
  done
  cells="$cells$C_RESET$C_EMPTY"
  while [ "$cell" -lt "$width" ]; do
    cells="$cells$EMPTY_CELL"
    cell=$((cell + 1))
  done
  BAR_HEAD="[$(date '+%H:%M:%S')] $prefix "
  BAR_TAIL=" $cells$C_RESET  $C_TEXT$(printf '%3d' "$percent")%$C_RESET  $text"
  BAR_COMPLETE=""
  [ "$percent" -lt 100 ] || BAR_COMPLETE="yes"
  BAR_ACTIVE="yes"
  BAR_OWNER="$name"
  start_spinner
}

# Every call of the Azure CLI goes through here. Warnings are suppressed and the CLI never
# reads the input of this script.
azure() {
  az "$@" --only-show-errors < /dev/null
}

is_set() {
  [ -n "${!1:-}" ]
}

cleanup() {
  stop_spinner
  [ -z "$TTY_STATE" ] || stty "$TTY_STATE" 2> /dev/null || true
  [ -z "$WORK" ] || rm -rf "$WORK"
}

interrupted() {
  end_bar
  echo
  if [ -n "$WATCHING" ]; then
    echo "Stopped watching. The deployment '$DEPLOYMENT_NAME' continues in Azure."
    echo "Watch it again with: make watch RESOURCE_GROUP=$RESOURCE_GROUP"
  elif [ -n "$ASKING" ]; then
    echo "Stopped. $NOTHING_DONE"
  elif [ -n "$STOP_NOTE" ]; then
    echo "$STOP_NOTE"
  fi
  exit 130
}

require_azure_cli() {
  [ -z "$SUBSCRIPTION" ] || return 0
  command -v az > /dev/null 2>&1 ||
    die "The Azure CLI (az) is not installed. See https://learn.microsoft.com/cli/azure/install-azure-cli"
  SUBSCRIPTION="$(azure account show --query "join(' / ', [name, id])" --output tsv 2> /dev/null)" ||
    die "Not signed in to Azure. Run 'az login' and, for another subscription, 'az account set --subscription <name or id>'."
}

require_deployment_name() {
  if [ -z "$DEPLOYMENT_NAME" ]; then
    if [ -z "$RESOURCE_GROUP" ] && [ -f "$ANSWERS_FILE" ]; then
      load_answers
      RESOURCE_GROUP="$(value_in "$SAVED" RESOURCE_GROUP)" || RESOURCE_GROUP=""
      check_group "$RESOURCE_GROUP" > /dev/null || RESOURCE_GROUP=""
      [ -z "$RESOURCE_GROUP" ] || echo "Resource group '$RESOURCE_GROUP', from the answers in $ANSWERS_FILE."
    fi
    [ -n "$RESOURCE_GROUP" ] || die "Set RESOURCE_GROUP, for example: make $ACTION RESOURCE_GROUP=<resource-group-name>"
    DEPLOYMENT_NAME="nss-$RESOURCE_GROUP"
    DEPLOYMENT_NAME="${DEPLOYMENT_NAME:0:64}"
  fi
}

# ---------------------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------------------

# Prints the value of a name in name=value lines, the last one when there are several.
# Fails when the name is not there.
value_in() {
  local line value="" found=""
  while IFS= read -r line; do
    case "$line" in
      "$2="*)
        value="${line#*=}"
        found="yes"
        ;;
    esac
  done << EOF
$1
EOF
  [ -n "$found" ] || return 1
  printf '%s' "$value"
}

# A name with a lower-case letter is a parameter of main.sub.bicep. A name in capitals is a
# setting of this script or an environment variable.
is_parameter() {
  case "$1" in
    *[[:lower:]]*) return 0 ;;
  esac
  return 1
}

# Prints the value PARAMETERS gives a parameter. Fails when it gives none: only then the
# parameter is asked for.
given_value() {
  local pair value="" found=""
  is_parameter "$1" || return 1
  for pair in $PARAMETERS; do
    case "$pair" in
      "$1="*)
        value="${pair#*=}"
        found="yes"
        ;;
    esac
  done
  [ -n "$found" ] || return 1
  printf '%s' "$value"
}

# Prints the value in use: the one PARAMETERS gives, otherwise the answer.
current() {
  given_value "$1" || value_in "$ANSWERS" "$1" || true
}

# Prints the value a parameter has in the parameter file, when it is written there as a
# text or as true or false. These values are the defaults of the questions.
file_value() {
  sed -n -E "s/^param[[:space:]]+$1[[:space:]]*=[[:space:]]*('([^']*)'|(true|false))[[:space:]]*(\/\/.*)?$/\2\3/p" \
    "$PARAMETER_FILE" | head -n 1
}

keep() {
  ANSWERS="$ANSWERS$1=$2"$'\n'
}

# Reads the answers file into SAVED. Only names this script saves are read, and the file is
# never run as a script.
load_answers() {
  local line name
  SAVED=""
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in
      "" | "#"* | *[[:cntrl:]]*) continue ;;
      *=*) ;;
      *) continue ;;
    esac
    name="${line%%=*}"
    case " $ANSWER_NAMES " in
      *" $name "*) SAVED="$SAVED$line"$'\n' ;;
    esac
  done < "$ANSWERS_FILE"
}

save_answers() {
  local name value
  {
    echo "# Answers to the questions of 'make deploy' and scripts/deploy.ps1, saved on $(date '+%Y-%m-%d %H:%M')."
    echo "# The next run offers them again. Secrets are never saved here. Delete this file to start over."
    for name in $ANSWER_NAMES; do
      if value="$(value_in "$ANSWERS" "$name")"; then
        printf '%s=%s\n' "$name" "$value"
      fi
    done
  } > "$ANSWERS_FILE" || die "The answers could not be saved in '$ANSWERS_FILE'."
  echo "The answers are saved in $ANSWERS_FILE, without the secrets."
}

# Reads one line that was typed into REPLY_TEXT, without the spaces around it.
read_reply() {
  IFS= read -r REPLY_TEXT || {
    echo
    die "The input ended before every question was answered. $NOTHING_DONE"
  }
  REPLY_TEXT="${REPLY_TEXT%$'\r'}"
  REPLY_TEXT="${REPLY_TEXT#"${REPLY_TEXT%%[![:space:]]*}"}"
  REPLY_TEXT="${REPLY_TEXT%"${REPLY_TEXT##*[![:space:]]}"}"
}

# Before a question: ends the run where nothing can be asked, and notes that the answers
# have to be saved again.
must_ask() {
  [ -n "$CAN_ASK" ] ||
    die "$1 is not answered in $ANSWERS_FILE, and this is not a terminal to ask it in. Run this in a terminal, or set RESOURCE_GROUP and give the values in PARAMETERS."
  ASKED="yes"
}

# After answers that do not fit together: says why and asks them again.
ask_again() {
  [ -n "$CAN_ASK" ] || die "$1 Correct the values in $ANSWERS_FILE, or run this in a terminal."
  echo "  $1"
  ACCEPT_SAVED=""
}

# Asks a question that is answered with yes or no. Succeeds for yes.
#   yes_no <question> <true or false, the answer of the Enter key>
yes_no() {
  local hint="y/N"
  [ "$2" != "true" ] || hint="Y/n"
  while :; do
    printf '%s [%s]: ' "$1" "$hint"
    read_reply
    case "$REPLY_TEXT" in
      "") [ "$2" = "true" ] && return 0 || return 1 ;;
      y | Y | yes | Yes | YES) return 0 ;;
      n | N | no | No | NO) return 1 ;;
    esac
    echo "  Answer yes or no."
  done
}

# Asks for one value and keeps it. A value PARAMETERS gives is not asked for.
#   ask <name> <question> <function that checks the value> [<default>] [optional]
# The default is used when nothing is saved. An optional value may be empty.
ask() {
  local name="$1" question="$2" check="$3" default="${4:-}" optional="${5:-}" saved problem="" shown
  given_value "$name" > /dev/null && return 0
  if saved="$(value_in "$ANSWERS" "$name")"; then
    default="$saved"
  elif saved="$(value_in "$SAVED" "$name")"; then
    if { [ -z "$saved" ] && [ -n "$optional" ]; } || { [ -n "$saved" ] && problem="$("$check" "$saved")"; }; then
      if [ -n "$ACCEPT_SAVED" ]; then
        keep "$name" "$saved"
        return 0
      fi
      default="$saved"
    elif [ -n "$problem" ]; then
      echo "  The saved value of $name cannot be used. $problem"
    fi
  fi
  must_ask "$name"
  while :; do
    shown="$default"
    [ -z "$optional" ] || [ -z "$default" ] || shown="$default, - for none"
    if [ -n "$shown" ]; then
      printf '%s [%s]: ' "$question" "$shown"
    else
      printf '%s: ' "$question"
    fi
    read_reply
    if [ -z "$REPLY_TEXT" ]; then
      REPLY_TEXT="$default"
    elif [ -n "$optional" ] && [ "$REPLY_TEXT" = "-" ]; then
      REPLY_TEXT=""
    fi
    if [ -z "$REPLY_TEXT" ]; then
      [ -z "$optional" ] || break
      echo "  A value is needed."
      continue
    fi
    if problem="$("$check" "$REPLY_TEXT")"; then
      break
    fi
    echo "  $problem"
  done
  keep "$name" "$REPLY_TEXT"
}

# Asks to choose one of several values and keeps it. An option is "value" or "value|description".
#   choose <name> <title> <default> <option>...
choose() {
  local name="$1" title="$2" default="$3" saved option value index default_index=1 chosen width=0
  shift 3
  given_value "$name" > /dev/null && return 0
  if saved="$(value_in "$ANSWERS" "$name")" || saved="$(value_in "$SAVED" "$name")"; then
    for option in "$@"; do
      if [ "${option%%|*}" = "$saved" ]; then
        if [ -n "$ACCEPT_SAVED" ]; then
          keep "$name" "$saved"
          return 0
        fi
        default="$saved"
      fi
    done
  fi
  must_ask "$name"
  for option in "$@"; do
    value="${option%%|*}"
    [ "${#value}" -le "$width" ] || width="${#value}"
  done
  echo "$title"
  index=0
  for option in "$@"; do
    index=$((index + 1))
    value="${option%%|*}"
    [ "$value" != "$default" ] || default_index="$index"
    case "$option" in
      *"|"*) printf '  %2d) %-*s  %s\n' "$index" "$width" "$value" "${option#*|}" ;;
      *) printf '  %2d) %s\n' "$index" "$value" ;;
    esac
  done
  while :; do
    printf 'Choose 1-%d [%d]: ' "$#" "$default_index"
    read_reply
    [ -n "$REPLY_TEXT" ] || REPLY_TEXT="$default_index"
    chosen=""
    index=0
    for option in "$@"; do
      index=$((index + 1))
      value="${option%%|*}"
      if [ "$REPLY_TEXT" = "$index" ] || [ "$REPLY_TEXT" = "$value" ]; then
        chosen="$value"
      fi
    done
    [ -z "$chosen" ] || break
    echo "  Enter a number from 1 to $#."
  done
  keep "$name" "$chosen"
}

# Asks a yes or no question and keeps the answer as true or false.
#   ask_yes_no <name> <question> <default>
ask_yes_no() {
  local name="$1" question="$2" default="${3:-false}" saved
  given_value "$name" > /dev/null && return 0
  if saved="$(value_in "$ANSWERS" "$name")" || saved="$(value_in "$SAVED" "$name")"; then
    case "$saved" in
      true | false)
        if [ -n "$ACCEPT_SAVED" ]; then
          keep "$name" "$saved"
          return 0
        fi
        default="$saved"
        ;;
    esac
  fi
  must_ask "$name"
  if yes_no "$question" "$default"; then
    keep "$name" "true"
  else
    keep "$name" "false"
  fi
}

# Asks for a value the parameter file reads from an environment variable that is not a
# secret, and sets the variable for this run. A variable that is set is not asked for.
#   ask_variable <name> <question> <function that checks the value> [optional]
ask_variable() {
  local value
  ! is_set "$1" || return 0
  ask "$1" "$2" "$3" "" "${4:-}"
  value="$(value_in "$ANSWERS" "$1")" || value=""
  [ -z "$value" ] || export "$1=$value"
}

hide_input() {
  TTY_STATE="$(stty -g 2> /dev/null)" || TTY_STATE=""
  stty -echo 2> /dev/null || true
}

show_input() {
  [ -z "$TTY_STATE" ] || stty "$TTY_STATE" 2> /dev/null || true
  TTY_STATE=""
}

# Asks for a secret without showing it and sets its environment variable for this run. It is
# never saved and never printed. A variable that is set is not asked for.
#   ask_secret <name> <question> [<function that checks the value>]
ask_secret() {
  local name="$1" question="$2" check="${3:-}" value problem
  ! is_set "$name" || return 0
  [ -n "$CAN_ASK" ] ||
    die "$name is not set, and this is not a terminal to ask for it in. Export it first: export $name='<value>'"
  # The terminal stops showing what is typed before the question appears, so that nothing
  # typed early is shown either.
  hide_input
  while :; do
    printf '%s (not shown, not saved): ' "$question"
    IFS= read -r -s value || {
      show_input
      echo
      die "The input ended before every question was answered. $NOTHING_DONE"
    }
    echo
    value="${value%$'\r'}"
    if [ -z "$value" ]; then
      echo "  A value is needed."
      continue
    fi
    if [ -n "$check" ] && ! problem="$("$check" "$value")"; then
      echo "  $problem"
      continue
    fi
    break
  done
  show_input
  export "$name=$value"
}

# The functions below check a value. For a value that cannot be used they print why and fail.

# Letters and digits are written out in the patterns, because in some locales a range such
# as A-Z also holds lower-case letters.
UPPER="ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LOWER="abcdefghijklmnopqrstuvwxyz"
DIGITS="0123456789"
ALNUM="$UPPER$LOWER$DIGITS"

matches() {
  local pattern="$2"
  [[ "$1" =~ $pattern ]]
}

check_group() {
  if ! matches "$1" "^[${ALNUM}_.()-]{1,90}\$" || [ "${1%.}" != "$1" ]; then
    echo "A resource group name has 1 to 90 letters, digits, hyphens, underscores, periods and parentheses, and does not end with a period."
    return 1
  fi
}

check_location() {
  if ! matches "$1" "^[${LOWER}${DIGITS}]+\$"; then
    echo "A region is written in lower-case letters and digits, for example eastus."
    return 1
  fi
  if [ -n "$REGIONS" ] && ! grep -qx "$1" <<< "$REGIONS"; then
    echo "'$1' is not a region of this subscription. The regions are: $(printf '%s\n' "$REGIONS" | tr '\n' ' ')"
    return 1
  fi
}

check_vm_name() {
  if ! matches "$1" "^[${ALNUM}][${ALNUM}._-]{0,63}\$" || matches "$1" '[.-]$'; then
    echo "A VM name has 1 to 64 letters, digits, hyphens, underscores and periods, starts with a letter or digit and does not end with a hyphen or period."
    return 1
  fi
}

check_network_name() {
  if ! matches "$1" "^[${ALNUM}][${ALNUM}._-]{0,62}[${ALNUM}_]\$"; then
    echo "A virtual network name has 2 to 64 letters, digits, hyphens, underscores and periods, starts with a letter or digit and ends with a letter, digit or underscore."
    return 1
  fi
}

check_subnet_name() {
  if ! matches "$1" "^[${ALNUM}]([${ALNUM}._-]{0,78}[${ALNUM}_])?\$"; then
    echo "A subnet name has 1 to 80 letters, digits, hyphens, underscores and periods, starts with a letter or digit and ends with a letter, digit or underscore."
    return 1
  fi
}

check_storage_name() {
  if ! matches "$1" "^[${LOWER}${DIGITS}]{3,24}\$"; then
    echo "A storage account name has 3 to 24 lower-case letters and digits."
    return 1
  fi
}

check_server_name() {
  if ! matches "$1" "^[${ALNUM}][${ALNUM} ._-]{0,254}\$"; then
    echo "Use letters, digits, spaces, hyphens, underscores and periods, starting with a letter or digit. Another name can be set in the parameter file."
    return 1
  fi
}

check_domain() {
  if ! matches "$1" "^[${ALNUM}][${ALNUM}.-]{0,252}\$"; then
    echo "Use letters, digits, hyphens and periods, for example acme."
    return 1
  fi
}

check_user_name() {
  if ! matches "$1" '^[^[:space:]]{1,254}$'; then
    echo "A user name has no spaces."
    return 1
  fi
}

check_sas_token() {
  case "$1" in
    *sig=*) ;;
    *)
      echo "This is not a SAS token: it has no sig= part. Paste the token as Zscaler Support sent it."
      return 1
      ;;
  esac
}

# Prints the first and the last address of an IPv4 range a.b.c.d/n as two numbers. Fails
# when the text is not such a range.
range_numbers() {
  local address="${1%/*}" bits="${1##*/}" part number=0 count=0 parts
  case "$1" in
    */*) ;;
    *) return 1 ;;
  esac
  matches "$address" "^[${DIGITS}]{1,3}\\.[${DIGITS}]{1,3}\\.[${DIGITS}]{1,3}\\.[${DIGITS}]{1,3}\$" || return 1
  matches "$bits" "^[${DIGITS}]{1,2}\$" || return 1
  bits=$((10#$bits))
  [ "$bits" -le 32 ] || return 1
  IFS=. read -r -a parts <<< "$address"
  for part in "${parts[@]}"; do
    part=$((10#$part))
    [ "$part" -le 255 ] || return 1
    number=$((number * 256 + part))
    count=$((count + 1))
  done
  [ "$count" -eq 4 ] || return 1
  echo "$number $((number + (1 << (32 - bits)) - 1))"
}

address_text() {
  echo "$(($1 >> 24 & 255)).$(($1 >> 16 & 255)).$(($1 >> 8 & 255)).$(($1 & 255))"
}

check_range() {
  local numbers first size
  if ! numbers="$(range_numbers "$1")"; then
    echo "An address range is written as an IPv4 address and a prefix length, for example 192.168.100.0/24."
    return 1
  fi
  first="${numbers% *}"
  size=$((${numbers#* } - first + 1))
  if [ $((first % size)) -ne 0 ]; then
    echo "$1 does not start at the first address of a range. The range it is in is $(address_text $((first - first % size)))/${1##*/}."
    return 1
  fi
}

check_ssh_source() {
  [ "$1" != "*" ] || return 0
  if matches "$1" "^[${DIGITS}.]+\$"; then
    range_numbers "$1/32" > /dev/null || {
      echo "This is not an IPv4 address. Enter an address such as 203.0.113.10, a range such as 203.0.113.0/24, or *."
      return 1
    }
    return 0
  fi
  check_range "$1"
}

# Checks that subnets lie inside a virtual network and do not overlap.
#   check_layout <range of the virtual network> <range of a subnet>...
check_layout() {
  local network="$1" numbers first last range index other
  local ranges=() firsts=() lasts=()
  numbers="$(range_numbers "$network")" || return 0
  first="${numbers% *}"
  last="${numbers#* }"
  shift
  for range in "$@"; do
    numbers="$(range_numbers "$range")" || continue
    if [ "${numbers% *}" -lt "$first" ] || [ "${numbers#* }" -gt "$last" ]; then
      echo "The subnet $range is not inside the virtual network $network."
      return 1
    fi
    index=0
    while [ "$index" -lt "${#ranges[@]}" ]; do
      if [ "${numbers% *}" -le "${lasts[$index]}" ] && [ "${numbers#* }" -ge "${firsts[$index]}" ]; then
        other="${ranges[$index]}"
        echo "The subnets $other and $range overlap."
        return 1
      fi
      index=$((index + 1))
    done
    ranges+=("$range")
    firsts+=("${numbers% *}")
    lasts+=("${numbers#* }")
  done
}

# Finds out once whether the resource group exists: GROUP_MODE and GROUP_LOCATION.
read_group() {
  [ "$GROUP_READ" != "$RESOURCE_GROUP" ] || return 0
  local exists group
  exists="$(azure group exists --name "$RESOURCE_GROUP" --output tsv)" ||
    die "Could not check whether the resource group '$RESOURCE_GROUP' exists."
  if [ "$exists" = "true" ]; then
    group="$(azure group show --name "$RESOURCE_GROUP" --query "join(' ', [location, properties.provisioningState])" --output tsv)" ||
      die "Could not read the resource group '$RESOURCE_GROUP'."
    # A group that is being deleted still exists, and a deployment into it fails.
    if [ "${group#* }" = "Deleting" ]; then
      die "The resource group '$RESOURCE_GROUP' is being deleted. Run this again when 'az group exists --name $RESOURCE_GROUP' answers false."
    fi
    GROUP_MODE="existing"
    GROUP_LOCATION="${group%% *}"
  else
    GROUP_MODE="new"
    GROUP_LOCATION=""
  fi
  GROUP_READ="$RESOURCE_GROUP"
}

# Finds the IP address this computer has on the internet: PUBLIC_ADDRESS, and the service
# that answered in PUBLIC_ADDRESS_FROM. Fails when no service answers with an IPv4 address.
# Nothing but such an address is taken from an answer.
public_address() {
  local url reply numbers
  PUBLIC_ADDRESS=""
  [ "$PUBLIC_IP_URL" != "none" ] || return 1
  command -v curl > /dev/null 2>&1 || return 1
  for url in $PUBLIC_IP_URL; do
    # The status follows the answer on a line of its own. Only an answer with status 200
    # counts, so not the text of a redirection either.
    reply="$(curl --silent --fail --connect-timeout 3 --max-time 5 --write-out '\n%{http_code}' "$url" 2> /dev/null < /dev/null)" || continue
    [ "${reply##*$'\n'}" = "200" ] || continue
    reply="${reply%$'\n'*}"
    reply="${reply%$'\r'}"
    reply="${reply#"${reply%%[![:space:]]*}"}"
    reply="${reply%"${reply##*[![:space:]]}"}"
    matches "$reply" "^[${DIGITS}.]{7,15}\$" || continue
    numbers="$(range_numbers "$reply/32")" || continue
    PUBLIC_ADDRESS="$(address_text "${numbers% *}")"
    PUBLIC_ADDRESS_FROM="$url"
    return 0
  done
  return 1
}

# Asks which addresses may reach the VM on SSH, and offers the public IP address of this
# computer. The address is only looked up when the question is asked.
ask_ssh_source() {
  local saved="" offered=""
  if [ -n "$CAN_ASK" ] && ! given_value sshSourceAddressPrefix > /dev/null; then
    saved="$(value_in "$SAVED" sshSourceAddressPrefix)" || saved=""
    if [ -n "$saved" ] && ! check_ssh_source "$saved" > /dev/null; then
      saved=""
    fi
    if [ -z "$saved" ] || [ -z "$ACCEPT_SAVED" ]; then
      if public_address; then
        offered="$PUBLIC_ADDRESS"
        if [ -z "$saved" ] || [ "$saved" = "$offered" ]; then
          echo "  Your public IP address is $offered, as $PUBLIC_ADDRESS_FROM sees it. Enter allows SSH from this address only."
        else
          echo "  Your public IP address is $offered, as $PUBLIC_ADDRESS_FROM sees it. The answer of the last run is $saved."
        fi
      fi
    fi
  fi
  ask sshSourceAddressPrefix "IP address or range that may reach the VM on SSH, for example 203.0.113.10 or 203.0.113.0/24 (* is every address)" check_ssh_source "$offered"
}

# Asks for the address ranges of a new virtual network until they fit together.
ask_ranges() {
  local problem
  while :; do
    ask vNetAddressRange "Address range of the virtual network" check_range "$(file_value vNetAddressRange)"
    ask subnetAddressRange "Address range of the service subnet" check_range "$(file_value subnetAddressRange)"
    ask subnetAddressRange2 "Address range of the management subnet" check_range "$(file_value subnetAddressRange2)"
    if problem="$(check_layout "$(current vNetAddressRange)" "$(current subnetAddressRange)" "$(current subnetAddressRange2)")"; then
      break
    fi
    ask_again "$problem"
  done
}

ask_certificate_range() {
  local problem
  while :; do
    ask certificateSubnetAddressRange "Address range of that subnet" check_range "$(file_value certificateSubnetAddressRange)"
    if problem="$(check_layout "$(current vNetAddressRange)" "$(current subnetAddressRange)" "$(current subnetAddressRange2)" "$(current certificateSubnetAddressRange)")"; then
      break
    fi
    ask_again "$problem"
  done
}

# True when one set of Zscaler API variables is complete.
zscaler_credentials_complete() {
  { is_set ZSCALER_CLIENT_ID && is_set ZSCALER_VANITY_DOMAIN && { is_set ZSCALER_CLIENT_SECRET || is_set ZSCALER_PRIVATE_KEY; }; } ||
    { is_set ZIA_USERNAME && is_set ZIA_PASSWORD && is_set ZIA_API_KEY && is_set ZIA_CLOUD; }
}

# Finds out which Zscaler API is used, from the variables that are set or with a question,
# and asks for its values that are not secrets: API_KIND.
#   ask_api <what the credentials are for>
ask_api() {
  if is_set ZSCALER_CLIENT_ID; then
    API_KIND="oneapi"
  elif is_set ZIA_PASSWORD || is_set ZIA_API_KEY; then
    API_KIND="legacy"
  else
    choose ZSCALER_API "Zscaler API credentials $1:" "oneapi" \
      "oneapi|OneAPI, for tenants on Zidentity" "legacy|Legacy ZIA API"
    API_KIND="$(value_in "$ANSWERS" ZSCALER_API)"
  fi
  if [ "$API_KIND" = "oneapi" ]; then
    ask_variable ZSCALER_VANITY_DOMAIN "Zidentity vanity domain, for example acme for acme.zslogin.net" check_domain
    ask_variable ZSCALER_CLOUD "Zscaler cloud, Enter for the production cloud" check_domain optional
  else
    ask_variable ZIA_USERNAME "Admin user name for the legacy ZIA API" check_user_name
    ask_variable ZIA_CLOUD "ZIA cloud name, for example zscalertwo" check_domain
  fi
}

# Asks for the secrets of the Zscaler API that ask_api found.
ask_api_secrets() {
  if [ "$API_KIND" = "oneapi" ]; then
    ask_secret ZSCALER_CLIENT_ID "OneAPI client ID"
    is_set ZSCALER_PRIVATE_KEY || ask_secret ZSCALER_CLIENT_SECRET "OneAPI client secret"
  else
    ask_secret ZIA_PASSWORD "Admin password for the legacy ZIA API"
    ask_secret ZIA_API_KEY "API key for the legacy ZIA API"
  fi
}

# Asks for everything a deployment needs that was not given, or takes it from the answers
# of the last run, and sets RESOURCE_GROUP and LOCATION. Secrets come last and are not saved.
ask_questions() {
  local certificate
  ASKING="yes"

  if [ -f "$ANSWERS_FILE" ]; then
    load_answers
  fi
  if [ -z "$CAN_ASK" ]; then
    echo "RESOURCE_GROUP is not set. The values are taken from the answers in $ANSWERS_FILE."
  else
    echo "RESOURCE_GROUP is not set, so the values of the deployment are asked for here."
    if [ -n "$SAVED" ]; then
      echo
      echo "Answers of the last run, from $ANSWERS_FILE:"
      printf '%s' "$SAVED" | sed 's/=/ = /; s/^/  /'
      if yes_no "Use these answers again?" true; then
        ACCEPT_SAVED="yes"
      fi
    fi
    if [ -z "$ACCEPT_SAVED" ]; then
      echo "Enter keeps the value in [brackets]. Ctrl+C stops. Nothing is deployed before you confirm at the end."
      echo
    fi
  fi

  ask RESOURCE_GROUP "Resource group, used as it is when it exists and created when it does not" check_group "$(file_value newResourceGroupName)"
  RESOURCE_GROUP="$(value_in "$ANSWERS" RESOURCE_GROUP)"
  read_group
  if [ "$GROUP_MODE" = "existing" ]; then
    [ -n "$ACCEPT_SAVED" ] || echo "  $RESOURCE_GROUP exists in $GROUP_LOCATION and is used as it is."
  else
    [ -n "$ACCEPT_SAVED" ] || echo "  $RESOURCE_GROUP does not exist and will be created."
    if [ -z "$LOCATION" ]; then
      REGIONS="$(azure account list-locations --query "[?metadata.regionType=='Physical'].name" --output tsv 2> /dev/null)" || REGIONS=""
      ask LOCATION "Azure region for the resource group and everything in it, for example eastus" check_location
      LOCATION="$(value_in "$ANSWERS" LOCATION)"
    fi
  fi

  ask vmName "Name of the NSS VM" check_vm_name "$(file_value vmName)"
  choose virtualMachineSize "Size of the VM:" "$(file_value virtualMachineSize)" "${VM_SIZE_OPTIONS[@]}"
  choose vhdRegion "Zscaler storage region the NSS image is copied from. It is the one your SAS token is for:" \
    "$(file_value vhdRegion)" "${VHD_REGION_OPTIONS[@]}"
  ask_ssh_source

  choose vNetNewOrExisting "Virtual network:" "$(file_value vNetNewOrExisting)" \
    "new|Create a new virtual network" "existing|Use an existing virtual network"
  if [ "$(current vNetNewOrExisting)" = "existing" ]; then
    ask virtualNetworkName "Name of the existing virtual network" check_network_name
    ask virtualNetworkResourceGroup "Resource group of that virtual network, Enter when it is $RESOURCE_GROUP" check_group "" optional
    ask subnetName "Name of its subnet for the service interface" check_subnet_name
    ask subnetName2 "Name of its subnet for the management interface" check_subnet_name
  else
    ask_yes_no NETWORK_DEFAULTS "Use the names and address ranges of the parameter file for it ($(file_value virtualNetworkName), $(file_value vNetAddressRange))?" true
    if [ "$(current NETWORK_DEFAULTS)" = "false" ]; then
      ask virtualNetworkName "Name of the virtual network" check_network_name "$(file_value virtualNetworkName)"
      ask subnetName "Name of the service subnet" check_subnet_name "$(file_value subnetName)"
      ask subnetName2 "Name of the management subnet" check_subnet_name "$(file_value subnetName2)"
      ask_ranges
    fi
  fi

  choose stgNewOrExisting "Storage account the NSS image is copied to:" "$(file_value stgNewOrExisting)" \
    "new|Create a new storage account" "existing|Use an existing storage account of $RESOURCE_GROUP"
  if [ "$(current stgNewOrExisting)" = "existing" ]; then
    ask nameStorageAccount "Name of the existing storage account" check_storage_name
  fi

  ask_yes_no installNssCertificate "Register the VM in ZIA, install its certificate and start NSS at the end of the deployment?" "$(file_value installNssCertificate)"
  certificate="$(current installNssCertificate)"
  if [ "$certificate" = "true" ]; then
    ask nssServerName "Name of the NSS server object in ZIA, reused when it exists and created when it does not" check_server_name "$(current vmName)"
    choose nssServerType "Type of the NSS server object, used when it is created:" "$(file_value nssServerType)" "${SERVER_TYPE_OPTIONS[@]}"
    if [ "$(current vNetNewOrExisting)" = "existing" ]; then
      if [ -z "$ACCEPT_SAVED" ]; then
        echo "The certificate step runs in a subnet of the virtual network. The subnet must exist, be delegated to"
        echo "Microsoft.ContainerInstance/containerGroups, have the Microsoft.Storage service endpoint and reach the internet."
      fi
      ask certificateSubnetName "Name of that subnet" check_subnet_name
    elif [ "$(current NETWORK_DEFAULTS)" = "false" ]; then
      ask certificateSubnetName "Name of the subnet the certificate step runs in" check_subnet_name "$(file_value certificateSubnetName)"
      ask_certificate_range
    fi

    ask_api "for the certificate step"
  fi

  if [ -n "$ASKED" ]; then
    echo
    save_answers
  fi

  if grep -q "readEnvironmentVariable('NSS_SAS_TOKEN')" "$PARAMETER_FILE"; then
    ask_secret NSS_SAS_TOKEN "SAS token of the NSS image, from Zscaler Support" check_sas_token
  fi
  if [ "$certificate" = "true" ]; then
    ask_secret NSS_VM_PASSWORD "Password of the zsroot account on the NSS VM"
    ask_api_secrets
  fi
  ASKING=""
  echo
}

# Decides between a new and an existing resource group and builds the arguments that
# 'az deployment sub what-if' and 'az deployment sub create' share.
prepare() {
  if [ -z "$RESOURCE_GROUP" ] && [ -z "$QUESTIONS" ]; then
    die "Set RESOURCE_GROUP, for example: make $ACTION RESOURCE_GROUP=<resource-group-name> LOCATION=<location>. In a terminal, 'make $ACTION' without it asks for the values."
  fi
  [ -f "$PARAMETER_FILE" ] || die "The parameter file '$PARAMETER_FILE' does not exist."
  grep -q "main\.sub\.bicep" "$PARAMETER_FILE" ||
    die "'$PARAMETER_FILE' is not a parameter file for main.sub.bicep. Its first line must be: using './main.sub.bicep'"
  if [ -n "$QUESTIONS" ]; then
    require_azure_cli
    ask_questions
  fi
  if grep -q "readEnvironmentVariable('NSS_SAS_TOKEN')" "$PARAMETER_FILE" && ! is_set NSS_SAS_TOKEN; then
    die "NSS_SAS_TOKEN is not set. Export the SAS token from Zscaler Support first: export NSS_SAS_TOKEN='<sas-token>'"
  fi

  # The certificate step runs last and fails without the password, after everything else
  # was deployed. When the parameter file takes the password from a variable that is not
  # set, that is known now.
  if parameter_is_true installNssCertificate && ! is_set NSS_VM_PASSWORD &&
    grep -q "readEnvironmentVariable('NSS_VM_PASSWORD'" "$PARAMETER_FILE" &&
    ! printf '%s\n' "$PARAMETERS" | grep -q "nssVmPassword="; then
    die "installNssCertificate is true and NSS_VM_PASSWORD is not set. Export the password of the zsroot account first: export NSS_VM_PASSWORD='<password>'"
  fi

  require_azure_cli
  require_deployment_name

  read_group
  if [ "$GROUP_MODE" = "existing" ]; then
    [ -n "$LOCATION" ] || LOCATION="$GROUP_LOCATION"
    DEPLOY_ARGUMENTS=("existingResourceGroupName=$RESOURCE_GROUP" "newResourceGroupName=")
  else
    [ -n "$LOCATION" ] ||
      die "The resource group '$RESOURCE_GROUP' does not exist. Set LOCATION to create it, for example: LOCATION=eastus"
    DEPLOY_ARGUMENTS=("existingResourceGroupName=" "newResourceGroupName=$RESOURCE_GROUP")
  fi

  # The answers come before the values of PARAMETERS, so a value given there wins.
  local name value pair
  for name in $ANSWER_NAMES; do
    is_parameter "$name" || continue
    if value="$(value_in "$ANSWERS" "$name")"; then
      DEPLOY_ARGUMENTS+=("$name=$value")
    fi
  done
  for pair in $PARAMETERS; do
    case "$pair" in
      *=*) DEPLOY_ARGUMENTS+=("$pair") ;;
      *) die "PARAMETERS must hold name=value pairs separated by spaces. This is not one: '${pair%%=*}'" ;;
    esac
  done
}

# True when the value the deployment will use for a boolean parameter is true: the last
# value given in PARAMETERS, otherwise the answer, otherwise the value in the parameter file.
parameter_is_true() {
  local name="$1" pair value=""
  for pair in $PARAMETERS; do
    case "$pair" in
      "$name="*) value="${pair#*=}" ;;
    esac
  done
  if [ -z "$value" ]; then
    value="$(value_in "$ANSWERS" "$name")" || value=""
  fi
  if [ -z "$value" ]; then
    grep -Eq "^param[[:space:]]+${name}[[:space:]]*=[[:space:]]*true" "$PARAMETER_FILE" && value="true"
  fi
  [ "$value" = "true" ]
}

inline_parameter_names() {
  local pair names=""
  for pair in $PARAMETERS; do
    names="$names ${pair%%=*}"
  done
  echo "${names:- (none)}"
}

summary() {
  local variable set_names="" unset_names=""
  for variable in $SECRET_VARIABLES; do
    if is_set "$variable"; then
      set_names="$set_names $variable"
    else
      unset_names="$unset_names $variable"
    fi
  done

  echo
  echo "  Subscription:       $SUBSCRIPTION"
  if [ "$GROUP_MODE" = "new" ]; then
    echo "  Resource group:     $RESOURCE_GROUP (does not exist, will be created)"
  else
    echo "  Resource group:     $RESOURCE_GROUP (exists, will be used as it is)"
  fi
  echo "  Location:           $LOCATION"
  echo "  Parameter file:     $PARAMETER_FILE"
  echo "  Replaced values:   $(inline_parameter_names)"
  if [ -n "$QUESTIONS" ]; then
    local name value label="Answered values:"
    for name in $ANSWER_NAMES; do
      is_parameter "$name" || continue
      if value="$(value_in "$ANSWERS" "$name")"; then
        printf '  %-19s %s=%s\n' "$label" "$name" "$value"
        label=""
      fi
    done
  fi
  echo "  Deployment name:    $DEPLOYMENT_NAME"
  echo "  Variables set:     ${set_names:- (none)}"
  echo "  Variables not set: ${unset_names:- (none)}"
  echo

  if parameter_is_true installNssCertificate; then
    if ! zscaler_credentials_complete; then
      echo "  WARNING: installNssCertificate is true and neither set of Zscaler API variables is complete."
      echo "           OneAPI: ZSCALER_CLIENT_ID, ZSCALER_VANITY_DOMAIN and ZSCALER_CLIENT_SECRET or ZSCALER_PRIVATE_KEY."
      echo "           Legacy: ZIA_USERNAME, ZIA_PASSWORD, ZIA_API_KEY and ZIA_CLOUD."
      echo "           The certificate step runs last. A missing value is only reported then, after the VM is deployed."
      echo
    fi
  fi
}

confirm() {
  case "$ASSUME_YES" in
    1 | y | yes | true) return 0 ;;
  esac
  [ -t 0 ] || die "This is not a terminal, so the confirmation cannot be asked. Set ASSUME_YES=1 to deploy without it."
  local answer
  printf 'Deploy now? [y/N] '
  read -r answer
  case "$answer" in
    y | Y | yes | YES | Yes) ;;
    *)
      echo "Nothing was deployed."
      exit 0
      ;;
  esac
}

state_of() {
  awk -F '\t' -v key="$1" '$1 == key { state = $2 } END { print state }' "$WORK/states"
}

is_finished() {
  case "$1" in
    Succeeded | Failed | Canceled) return 0 ;;
  esac
  return 1
}

indent() {
  printf '%*s' "$(($1 * 2))" ''
}

# Prints the new lines of the log of a deployment script. Progress lines of the VHD copy
# that follow each other are shown as one progress, the latest.
report_script_log() {
  local name="$1" depth="$2" file shown total line progress=""
  file="$WORK/log.$(printf '%s' "$name" | tr -c 'A-Za-z0-9._-' '_')"
  azure deployment-scripts show-log --resource-group "$TARGET_GROUP" --name "$name" \
    --query "value[0].log" --output tsv > "$file.raw" 2> /dev/null || return 0
  [ "$(cat "$file.raw")" != "None" ] || return 0
  # Azure returns the log of the certificate script as one line. That script starts every line
  # it writes with "==> ", and the log is cut into lines again at these marks.
  # The log ends with empty lines that must not be counted as lines that were shown.
  awk '{ sub(/\r$/, ""); sub(/^[[:space:]]*==> /, ""); gsub(/[[:space:]]*==> /, "\n"); print }' "$file.raw" |
    awk '{ sub(/[[:space:]]+$/, ""); lines[NR] = $0 }
    END { last = NR; while (last > 0 && lines[last] ~ /^[[:space:]]*$/) last--; for (i = 1; i <= last; i++) print lines[i] }' \
      > "$file"
  shown="$(cat "$file.shown" 2> /dev/null || echo 0)"
  total="$(wc -l < "$file" | tr -d ' ')"
  if [ "$total" -gt "$shown" ]; then
    tail -n "+$((shown + 1))" "$file" > "$file.new"
    while IFS= read -r line <&4; do
      if parse_progress "$line"; then
        progress="yes"
        continue
      fi
      if [ -n "$progress" ]; then
        show_progress "$name" "$depth"
        progress=""
      fi
      [ "$BAR_OWNER" != "$name" ] || end_bar
      say "$(indent "$depth")  | $line"
    done 4< "$file.new"
    [ -z "$progress" ] || show_progress "$name" "$depth"
    echo "$total" > "$file.shown"
  fi
}

# Prints the state of the operation report_operations is looking at, when it changed.
announce_change() {
  [ -n "$changed" ] || return 0
  say "$(indent "$depth")$label: $state"
  printf '%s\t%s\n' "$key" "$state" >> "$WORK/states"
  if [ "$state" = "Failed" ] && [ "$type" != "Microsoft.Resources/deployments" ]; then
    printf '%s\t%s\n' "$scope" "$deployment" >> "$WORK/failed"
  fi
}

# Reports every operation of a deployment whose state changed since the last check, and
# does the same for the deployments and deployment scripts it contains. A step that is
# still running is announced before its content, a step that ended after it.
#   report_operations subscription <deployment> 0
#   report_operations group <deployment> <depth>
report_operations() {
  local scope="$1" deployment="$2" depth="$3"
  local file="$WORK/operations.$3" type name state group key label changed
  if [ "$scope" = "subscription" ]; then
    azure deployment operation sub list --name "$deployment" \
      --query "$OPERATIONS_QUERY" --output tsv > "$file" 2> /dev/null || return 0
  else
    azure deployment operation group list --resource-group "$TARGET_GROUP" --name "$deployment" \
      --query "$OPERATIONS_QUERY" --output tsv > "$file" 2> /dev/null || return 0
  fi

  while IFS=$'\t' read -r type name state group <&3; do
    if [ -z "$type" ] || [ "$type" = "None" ]; then
      continue
    fi
    key="$depth|$type|$name"

    case "$type" in
      Microsoft.Resources/resourceGroups) label="resource group $name" ;;
      Microsoft.Resources/deployments)
        if [ "$scope" = "subscription" ]; then
          label="deployment into resource group $group"
        else
          label="step $(printf '%s' "$name" | sed -E 's/-[a-z0-9]{13}$//')"
        fi
        ;;
      *) label="$name (${type##*/})" ;;
    esac

    changed=""
    [ "$state" = "$(state_of "$key")" ] || changed="yes"
    grep -Fxq "$key" "$WORK/finished" && continue

    is_finished "$state" || announce_change
    case "$type" in
      Microsoft.Resources/deployments)
        [ "$scope" = "subscription" ] && [ "$group" != "None" ] && TARGET_GROUP="$group"
        [ -z "$TARGET_GROUP" ] || report_operations group "$name" "$((depth + 1))"
        ;;
      Microsoft.Resources/deploymentScripts)
        report_script_log "$name" "$depth"
        if is_finished "$state" && [ "$BAR_OWNER" = "$name" ]; then
          end_bar
        fi
        ;;
    esac
    if is_finished "$state"; then
      announce_change
      echo "$key" >> "$WORK/finished"
    fi
  done 3< "$file"
}

# Lists the operations of a deployment, at the scope it was deployed to.
list_operations() {
  local scope="$1" deployment="$2" query="$3"
  if [ "$scope" = "subscription" ]; then
    azure deployment operation sub list --name "$deployment" --query "$query" --output tsv 2> /dev/null || true
  else
    azure deployment operation group list --resource-group "$TARGET_GROUP" --name "$deployment" \
      --query "$query" --output tsv 2> /dev/null || true
  fi
}

report_failures() {
  local scope deployment name code
  echo
  [ ! -s "$WORK/failed" ] || echo "What failed:"
  sort -u "$WORK/failed" | while IFS=$'\t' read -r scope deployment; do
    list_operations "$scope" "$deployment" "$FAILURES_QUERY" | while IFS=$'\t' read -r name code; do
      echo "  $name: $code"
      # The message can have several lines, so it is read on its own.
      list_operations "$scope" "$deployment" \
        "[?properties.provisioningState=='Failed' && properties.targetResource.resourceName=='$name'] | [0].properties.statusMessage.error.message" |
        sed -e '/^[[:space:]]*$/d' -e 's/^/    /'
    done
  done
  echo
  echo "Full error of the deployment:"
  azure deployment sub show --name "$DEPLOYMENT_NAME" --query properties.error --output yaml 2> /dev/null || true
  echo
  if ls "$WORK"/log.*.shown > /dev/null 2>&1; then
    echo "The lines above marked with | are the log of a deployment script. Azure keeps it for one day."
  fi
  echo "After correcting the cause, run the same command again. Resources that were deployed are kept and reused."
}

show_outputs() {
  local outputs
  outputs="$(print_outputs)" || die "The deployment '$DEPLOYMENT_NAME' cannot be read in subscription $SUBSCRIPTION."
  if [ -n "$outputs" ]; then
    echo "$outputs"
    case "$outputs" in
      *"nssRunning: false"*)
        echo
        echo "nssRunning is false: NSS is not running on the virtual machine. See 'Start NSS' under"
        echo "'After the deployment' in README.md."
        ;;
    esac
  else
    echo "The deployment '$DEPLOYMENT_NAME' has no outputs. Outputs exist after a deployment that succeeded."
  fi
}

print_outputs() {
  azure deployment sub show --name "$DEPLOYMENT_NAME" --output yaml --query "properties.outputs && properties.outputs.{
    resourceGroupName: resourceGroupName.value,
    vmName: vmName.value,
    managementPublicIpAddress: managementPublicIpAddress.value,
    managementPrivateIpAddress: managementPrivateIpAddress.value,
    serviceInterfaceAddress: serviceInterfaceAddress.value,
    serviceInterfaceGateway: serviceInterfaceGateway.value,
    servicePublicIp: servicePublicIp.value,
    storageAccountName: storageAccountName.value,
    virtualNetworkName: virtualNetworkName.value,
    nssServerId: nssServerId.value,
    nssRunning: nssRunning.value
  }"
}

watch_deployment() {
  local state unreadable=0
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/nss-deploy.XXXXXX")"
  : > "$WORK/states"
  : > "$WORK/finished"
  : > "$WORK/failed"
  WATCHING="yes"
  setup_display

  echo "Progress is checked every $POLL_SECONDS seconds. Ctrl+C stops watching, not the deployment."
  while :; do
    state="$(azure deployment sub show --name "$DEPLOYMENT_NAME" --query properties.provisioningState --output tsv 2> /dev/null || true)"
    if [ -z "$state" ]; then
      unreadable=$((unreadable + 1))
      [ "$unreadable" -lt 5 ] ||
        die "The deployment '$DEPLOYMENT_NAME' cannot be read in subscription $SUBSCRIPTION."
    else
      unreadable=0
      report_operations subscription "$DEPLOYMENT_NAME" 0 || true
      if is_finished "$state"; then
        break
      fi
    fi
    sleep "$POLL_SECONDS"
  done
  end_bar
  WATCHING=""

  if [ "$state" = "Succeeded" ]; then
    say "The deployment succeeded."
    echo
    show_outputs
    echo
    echo "Next: see 'After the deployment' in README.md. A new NSS server object in ZIA is a pending"
    echo "change that this deployment does not activate."
    return 0
  fi
  say "The deployment ended with the state $state."
  report_failures
  return 1
}

# ---------------------------------------------------------------------------------------
# Destroy
# ---------------------------------------------------------------------------------------

RECORD_STATE=""
RECORD_NEW_GROUP=""
SERVER_ID=""
SERVER_NAME=""
SERVER_ID_FROM=""
ZIA_NOTE=""
GROUP_STATE=""      # Succeeded, Deleting or gone
GROUP_ACTION=""     # delete, keep or gone
GROUP_REASON=""
OURS=0
OTHERS=0
PYTHON_COMMAND=""

lower() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]'
}

is_yes() {
  case "$1" in
    1 | y | yes | true) return 0 ;;
  esac
  return 1
}

is_no() {
  case "$1" in
    0 | n | no | false) return 0 ;;
  esac
  return 1
}

# Reads the record of the deployment: its state, its resource group, and what it says
# about the NSS server object.
read_record() {
  local record existing new certificate name id group
  if ! record="$(azure deployment sub show --name "$DEPLOYMENT_NAME" --query "$RECORD_QUERY" --output tsv 2> "$WORK/error")"; then
    if grep -q "DeploymentNotFound" "$WORK/error"; then
      die "There is no deployment '$DEPLOYMENT_NAME' in subscription $SUBSCRIPTION. Destroy deletes what 'make deploy' or scripts/deploy.ps1 deployed, and finds it through the record of that deployment. Nothing was deleted."
    fi
    cat "$WORK/error" >&2
    die "The deployment '$DEPLOYMENT_NAME' cannot be read in subscription $SUBSCRIPTION. Nothing was deleted."
  fi
  IFS=$'\t' read -r RECORD_STATE existing new certificate name id << EOF
$record
EOF
  is_finished "$RECORD_STATE" ||
    die "The deployment '$DEPLOYMENT_NAME' has the state '$RECORD_STATE' and has not ended. Wait for it with 'make watch', or stop it with 'az deployment sub cancel --name $DEPLOYMENT_NAME', and run this again. Nothing was deleted."

  group="$existing"
  if [ "$group" = "-" ]; then
    group="$new"
    RECORD_NEW_GROUP="$new"
  fi
  if [ -z "$group" ] || [ "$group" = "-" ]; then
    die "The record of the deployment '$DEPLOYMENT_NAME' names no resource group, so it is not a deployment of main.sub.bicep. Nothing was deleted."
  fi
  if [ -n "$RESOURCE_GROUP" ] && [ "$(lower "$RESOURCE_GROUP")" != "$(lower "$group")" ]; then
    die "The deployment '$DEPLOYMENT_NAME' went into the resource group '$group', not into '$RESOURCE_GROUP'. Nothing was deleted."
  fi
  RESOURCE_GROUP="$group"

  [ "$name" = "-" ] || SERVER_NAME="$name"
  if [ "$certificate" != "true" ]; then
    ZIA_NOTE="none: the deployment did not register the VM in ZIA (installNssCertificate was not true)"
  elif [ "$id" != "-" ]; then
    SERVER_ID="$id"
    SERVER_ID_FROM="record"
  fi
}

# Writes the ID of everything the deployment created to $WORK/created, and does the same
# for the deployments it contains.
#   collect_created subscription <deployment> 0
#   collect_created group <deployment> <depth>
collect_created() {
  local scope="$1" deployment="$2" depth="$3"
  local file="$WORK/created.$3" type id
  if [ "$scope" = "subscription" ]; then
    azure deployment operation sub list --name "$deployment" \
      --query "$CREATED_QUERY" --output tsv > "$file" 2> /dev/null ||
      die "The operations of the deployment '$deployment' cannot be read. Nothing was deleted."
  elif ! azure deployment operation group list --resource-group "$RESOURCE_GROUP" --name "$deployment" \
    --query "$CREATED_QUERY" --output tsv > "$file" 2> /dev/null; then
    echo "$deployment" >> "$WORK/unread"
    return 0
  fi

  while IFS=$'\t' read -r type id <&3; do
    case "$type" in
      "" | None | Microsoft.Resources/resourceGroups) ;;
      Microsoft.Resources/deployments)
        # Only the deployments in the resource group of this deployment are followed.
        case "$(lower "$id")" in
          */resourcegroups/"$(lower "$RESOURCE_GROUP")"/providers/*)
            collect_created group "${id##*/}" "$((depth + 1))"
            ;;
        esac
        ;;
      *) echo "$id" >> "$WORK/created" ;;
    esac
  done 3< "$file"
}

# Reads what is in the resource group and tells what the deployment created from the
# rest: $WORK/ours and $WORK/others, as lines of ID, type and name, and OURS and OTHERS.
# Fails when the resource group cannot be listed.
read_inventory() {
  local id storage container server
  azure resource list --resource-group "$RESOURCE_GROUP" --query "[].[id, type, name]" --output tsv > "$WORK/present" ||
    return 1

  # A deployment script that failed leaves the storage account and the container it ran
  # in behind. Azure names them in the script, and they are deleted with it.
  : > "$WORK/supporting"
  if azure deployment-scripts list --resource-group "$RESOURCE_GROUP" --query "$SCRIPTS_QUERY" --output tsv > "$WORK/scripts" 2> /dev/null; then
    while IFS=$'\t' read -r id storage container server <&3; do
      [ -n "$id" ] || continue
      grep -Fixq "$id" "$WORK/created" || continue
      printf '%s\n%s\n' "$storage" "$container" >> "$WORK/supporting"
      # The ID in the record of the deployment is missing after a run that failed. The
      # certificate script of an earlier run that succeeded still has it.
      if [ -z "$SERVER_ID" ] && [ -z "$ZIA_NOTE" ] && [ "$server" != "-" ]; then
        SERVER_ID="$server"
        SERVER_ID_FROM="script"
      fi
    done 3< "$WORK/scripts"
  fi

  : > "$WORK/ours"
  : > "$WORK/others"
  awk -F '\t' -v group="$(lower "$RESOURCE_GROUP")" -v ours_file="$WORK/ours" -v others_file="$WORK/others" '
    FILENAME == ARGV[1] {
      # A resource of the resource group itself: not a part of another resource, and not
      # something attached to one, such as a role assignment.
      parts = split(tolower($1), part, "/")
      if (parts == 9 && part[4] == "resourcegroups" && part[5] == group && part[6] == "providers") ours[tolower($1)] = 1
      next
    }
    FILENAME == ARGV[2] { ours[tolower($1)] = 1; next }
    { if (tolower($1) in ours) print > ours_file; else print > others_file }
  ' "$WORK/created" "$WORK/supporting" "$WORK/present"
  OURS="$(wc -l < "$WORK/ours" | tr -d ' ')"
  OTHERS="$(wc -l < "$WORK/others" | tr -d ' ')"

  # What the deployment created inside a resource that is not its own, such as the
  # container with the copied VHD in an existing storage account.
  awk -F '\t' '
    FILENAME == ARGV[1] { ours[tolower($1)] = 1; next }
    {
      parts = split($1, part, "/")
      if (parts <= 9) next
      parent = ""
      for (i = 2; i <= 9; i++) parent = parent "/" part[i]
      if (!(tolower(parent) in ours)) print $1
    }
  ' "$WORK/ours" "$WORK/created" | sort -u > "$WORK/inside"
}

# Decides whether the resource group is deleted: GROUP_ACTION and GROUP_REASON.
decide_group() {
  if [ "$GROUP_STATE" = "gone" ]; then
    GROUP_ACTION="gone"
  elif [ "$GROUP_STATE" = "Deleting" ]; then
    GROUP_ACTION="delete"
    GROUP_REASON="it is already being deleted"
  elif [ "$OTHERS" -gt 0 ]; then
    GROUP_ACTION="keep"
    GROUP_REASON="it holds $OTHERS resources this deployment did not create"
    [ "$OTHERS" -ne 1 ] || GROUP_REASON="it holds 1 resource this deployment did not create"
  elif is_yes "$KEEP_RESOURCE_GROUP"; then
    GROUP_ACTION="keep"
    GROUP_REASON="KEEP_RESOURCE_GROUP is set"
  elif is_no "$KEEP_RESOURCE_GROUP"; then
    GROUP_ACTION="delete"
    GROUP_REASON="it holds nothing but this deployment, and KEEP_RESOURCE_GROUP is $KEEP_RESOURCE_GROUP"
  elif [ -n "$RECORD_NEW_GROUP" ]; then
    GROUP_ACTION="delete"
    GROUP_REASON="this deployment created it, and it holds nothing else"
  elif [ -n "$CAN_ASK" ]; then
    echo "The resource group '$RESOURCE_GROUP' holds nothing but this deployment, but it existed when the"
    echo "deployment last ran, so the record does not show whether 'make deploy' created it."
    ASKING="yes"
    if yes_no "Delete the resource group itself as well?" false; then
      GROUP_ACTION="delete"
      GROUP_REASON="it holds nothing but this deployment, and you chose to delete it"
    else
      GROUP_ACTION="keep"
      GROUP_REASON="you chose to keep it"
    fi
    ASKING=""
    echo
  else
    GROUP_ACTION="keep"
    GROUP_REASON="it existed when the deployment last ran. KEEP_RESOURCE_GROUP=0 deletes it"
  fi
}

python_is_new_enough() {
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' > /dev/null 2>&1 < /dev/null
}

# Finds the Python that runs scripts/delete-nss-server.py: PYTHON_COMMAND.
find_python() {
  local candidate
  if [ -n "$PYTHON" ]; then
    python_is_new_enough "$PYTHON" ||
      die "PYTHON is '$PYTHON', which is not Python 3.10 or later. Nothing was deleted."
    PYTHON_COMMAND="$PYTHON"
    return 0
  fi
  for candidate in python3 python python3.14 python3.13 python3.12 python3.11 python3.10; do
    if command -v "$candidate" > /dev/null 2>&1 && python_is_new_enough "$candidate"; then
      PYTHON_COMMAND="$candidate"
      return 0
    fi
  done
  die "The NSS server object is deleted with zscaler-sdk-python, which needs Python 3.10 or later, and none was found. Install it or set PYTHON to one, or set KEEP_NSS_SERVER=1 to leave the object in ZIA. Nothing was deleted."
}

# Decides what happens to the NSS server object, and gets what its deletion needs.
prepare_zia() {
  if [ -n "${NSS_SERVER_ID:-}" ]; then
    case "$NSS_SERVER_ID" in
      *[!0-9]*) die "NSS_SERVER_ID must be the ID of the NSS server, which is a number. Nothing was deleted." ;;
    esac
    SERVER_ID="$NSS_SERVER_ID"
    SERVER_ID_FROM="setting"
    ZIA_NOTE=""
  fi
  if is_yes "$KEEP_NSS_SERVER"; then
    [ -z "$SERVER_ID" ] || ZIA_NOTE="the object with the ID $SERVER_ID is kept, because KEEP_NSS_SERVER is set"
    SERVER_ID=""
    [ -n "$ZIA_NOTE" ] || ZIA_NOTE="nothing is deleted, because KEEP_NSS_SERVER is set"
    return 0
  fi
  if [ -z "$SERVER_ID" ]; then
    [ -n "$ZIA_NOTE" ] ||
      ZIA_NOTE="nothing is deleted: the deployment recorded no NSS server ID, because its certificate step did not finish"
    return 0
  fi

  find_python
  zscaler_credentials_complete && return 0
  [ -n "$CAN_ASK" ] ||
    die "The NSS server object $SERVER_ID has to be deleted in ZIA, and neither set of Zscaler API variables is complete. Export OneAPI (ZSCALER_CLIENT_ID, ZSCALER_VANITY_DOMAIN and ZSCALER_CLIENT_SECRET or ZSCALER_PRIVATE_KEY) or legacy (ZIA_USERNAME, ZIA_PASSWORD, ZIA_API_KEY and ZIA_CLOUD), or set KEEP_NSS_SERVER=1 to leave the object in ZIA. Nothing was deleted."
  echo "The NSS server object $SERVER_ID is deleted in ZIA with the Zscaler API credentials."
  ASKING="yes"
  if [ -f "$ANSWERS_FILE" ]; then
    load_answers
    ACCEPT_SAVED="yes"
  fi
  ask_api "to delete the NSS server object with"
  ask_api_secrets
  ASKING=""
  echo
  zscaler_credentials_complete ||
    die "The Zscaler API variables that are set do not make one complete set. Nothing was deleted."
}

# Prints the lines of ID, type and name of a file as type and name.
list_resources() {
  awk -F '\t' '{ printf "                      %s  %s\n", $2, $3 }' "$1" | sort
}

destroy_summary() {
  local step
  echo
  echo "  Subscription:       $SUBSCRIPTION"
  echo "  Deployment:         $DEPLOYMENT_NAME ($RECORD_STATE)"
  case "$GROUP_ACTION" in
    gone)
      echo "  Resource group:     $RESOURCE_GROUP no longer exists, so there is nothing to delete in Azure"
      ;;
    delete)
      echo "  Resource group:     $RESOURCE_GROUP is DELETED with everything in it, because $GROUP_REASON"
      if [ "$OURS" -gt 0 ]; then
        echo "  Deleted with it:"
        list_resources "$WORK/ours"
      fi
      ;;
    keep)
      echo "  Resource group:     $RESOURCE_GROUP is kept, because $GROUP_REASON"
      if [ "$OURS" -gt 0 ]; then
        echo "  DELETED in it:      the $OURS resources this deployment created"
        list_resources "$WORK/ours"
      else
        echo "  DELETED in it:      nothing. No resource this deployment created is left in it"
      fi
      if [ "$OTHERS" -gt 0 ]; then
        echo "  Kept in it:         what this deployment did not create"
        list_resources "$WORK/others"
      fi
      if [ -s "$WORK/inside" ]; then
        echo "  Also kept:          what the deployment created inside a resource that is not its own"
        sed 's/^/                      /' "$WORK/inside"
      fi
      ;;
  esac
  if [ -s "$WORK/unread" ]; then
    echo "  WARNING:            the record of these steps cannot be read, so what they created is not"
    echo "                      known to be of this deployment and is kept:"
    while IFS= read -r step; do
      echo "                      $(printf '%s' "$step" | sed -E 's/-[a-z0-9]{13}$//')"
    done < "$WORK/unread"
  fi
  if [ -n "$SERVER_ID" ]; then
    if [ "$SERVER_ID_FROM" = "setting" ]; then
      echo "  NSS server in ZIA:  the object with the ID $SERVER_ID, from NSS_SERVER_ID, is DELETED"
    else
      echo "  NSS server in ZIA:  '$SERVER_NAME' with the ID $SERVER_ID is DELETED"
    fi
    echo "                      The deletion is a pending change in ZIA that is not activated here."
  else
    echo "  NSS server in ZIA:  $ZIA_NOTE"
  fi
  echo "  Deployment record:  $DEPLOYMENT_NAME is deleted at the end"
  echo
}

confirm_destroy() {
  local answer
  if [ -n "$CONFIRM_DESTROY" ]; then
    [ "$CONFIRM_DESTROY" = "$RESOURCE_GROUP" ] ||
      die "CONFIRM_DESTROY is '$CONFIRM_DESTROY', which is not the name of the resource group, '$RESOURCE_GROUP'. Nothing was deleted."
    return 0
  fi
  [ -t 0 ] ||
    die "This is not a terminal, so the confirmation cannot be asked. Set CONFIRM_DESTROY=$RESOURCE_GROUP to destroy without it. Nothing was deleted."
  echo "This cannot be undone."
  ASKING="yes"
  printf 'Type the name of the resource group to delete what is listed above, anything else to stop: '
  IFS= read -r answer || answer=""
  ASKING=""
  if [ "$answer" != "$RESOURCE_GROUP" ]; then
    echo "Nothing was deleted."
    exit 0
  fi
}

# Deletes the NSS server object in ZIA with scripts/delete-nss-server.py.
delete_nss_server() {
  local python="$PYTHON_COMMAND" expected=""
  if ! "$python" -c 'import zscaler' > /dev/null 2>&1 < /dev/null; then
    say "Installing $SDK_REQUIREMENT into a temporary Python environment, which is removed afterwards."
    "$python" -m venv "$WORK/python" < /dev/null ||
      die "'$python -m venv' failed, so zscaler-sdk-python could not be installed. Set PYTHON to a Python 3.10 or later that has it. Nothing was deleted."
    python="$WORK/python/bin/python"
    "$python" -m pip install --quiet --disable-pip-version-check --no-cache-dir "$SDK_REQUIREMENT" < /dev/null ||
      die "$SDK_REQUIREMENT could not be installed. The message above is from pip. Nothing was deleted."
  fi
  # An ID that was given is taken as it is. The name the deployment recorded is only
  # compared for the ID the deployment recorded.
  [ "$SERVER_ID_FROM" = "setting" ] || expected="$SERVER_NAME"
  say "Deleting the NSS server object $SERVER_ID in ZIA."
  STOP_NOTE="Stopped while the NSS server object was being deleted in ZIA. Nothing was deleted in Azure. Run this again to finish."
  if ! NSS_SERVER_ID="$SERVER_ID" NSS_SERVER_NAME="$expected" "$python" "$REPOSITORY/scripts/delete-nss-server.py" 2>&1 < /dev/null | sed 's/^/  | /'; then
    die "The NSS server object was not deleted in ZIA, for the reason above, so nothing was deleted in Azure either. Correct the cause and run this again, give the ID with NSS_SERVER_ID=<id>, or set KEEP_NSS_SERVER=1 to leave the object in ZIA."
  fi
}

# Deletes the resource group and waits until it is gone.
delete_group() {
  local exists state count last="" unexpected=0 unreadable=0
  STOP_NOTE="Stopped watching. The deletion of the resource group '$RESOURCE_GROUP' continues in Azure. Run this again to finish: it then waits for the deletion and deletes the record of the deployment."
  if [ "$GROUP_STATE" != "Deleting" ]; then
    azure group delete --name "$RESOURCE_GROUP" --yes --no-wait > /dev/null ||
      die "The deletion of the resource group '$RESOURCE_GROUP' was not started. The message above is from Azure."
    say "The deletion of the resource group '$RESOURCE_GROUP' was started. It takes several minutes."
  else
    say "The resource group '$RESOURCE_GROUP' is being deleted. It takes several minutes."
  fi
  echo "Progress is checked every $POLL_SECONDS seconds. Ctrl+C stops watching, not the deletion."
  while :; do
    exists="$(azure group exists --name "$RESOURCE_GROUP" --output tsv 2> /dev/null)" || exists=""
    [ "$exists" != "false" ] || break
    if [ "$exists" != "true" ]; then
      unreadable=$((unreadable + 1))
      [ "$unreadable" -lt 5 ] ||
        die "The resource group '$RESOURCE_GROUP' cannot be read in subscription $SUBSCRIPTION, so it is not known whether it is deleted. Run this again."
    else
      unreadable=0
      state="$(azure group show --name "$RESOURCE_GROUP" --query properties.provisioningState --output tsv 2> /dev/null)" || state=""
      # Azure sets the state back when it could not delete the resource group.
      if [ -n "$state" ] && [ "$state" != "Deleting" ]; then
        unexpected=$((unexpected + 1))
        [ "$unexpected" -lt 3 ] ||
          die "Azure did not delete the resource group '$RESOURCE_GROUP': its state is '$state' again. The reason is in its activity log, for example a lock or a resource that cannot be deleted. Run this again after correcting it."
      else
        unexpected=0
      fi
      count="$(azure resource list --resource-group "$RESOURCE_GROUP" --query "length(@)" --output tsv 2> /dev/null)" || count=""
      if [ -n "$count" ] && [ "$count" != "$last" ]; then
        say "  resource group $RESOURCE_GROUP: $count resources left"
        last="$count"
      fi
    fi
    sleep "$POLL_SECONDS"
  done
  say "The resource group '$RESOURCE_GROUP' is deleted."
}

# Deletes the resources the deployment created and keeps the resource group. The Azure
# CLI deletes what depends on something else first; what is left is tried again for as
# long as a try deletes something.
delete_resources() {
  local ids id left="$OURS" before
  [ "$left" -gt 0 ] || return 0
  STOP_NOTE="Stopped. A deletion that Azure has started continues. Run this again to delete what is left."
  say "Deleting $left resources in the resource group '$RESOURCE_GROUP'. It takes several minutes, and nothing is printed while Azure deletes."
  while :; do
    ids=()
    # The VM and the deployment scripts first: most of the rest is in use by them.
    while IFS=$'\t' read -r id _; do
      ids+=("$id")
    done << EOF
$(awk -F '\t' '{ first = tolower($2) ~ /virtualmachines$|deploymentscripts$|containergroups$/ ? 0 : 1; print first "\t" $0 }' "$WORK/ours" | sort -s -k1,1 | cut -f2-)
EOF
    if azure resource delete --ids "${ids[@]}" > /dev/null; then
      break
    fi
    before="$left"
    read_inventory ||
      die "The resources of the resource group '$RESOURCE_GROUP' cannot be listed, so it is not known what is left. Run this again."
    left="$OURS"
    [ "$left" -gt 0 ] || break
    if [ "$left" -ge "$before" ]; then
      echo "Not deleted:"
      list_resources "$WORK/ours"
      die "Azure deleted none of the resources that are left, for the reason above. Correct it and run this again."
    fi
    say "  $left resources left. Trying those again."
  done
  say "The resources of the deployment in '$RESOURCE_GROUP' are deleted."
}

destroy() {
  WORK="$(mktemp -d "${TMPDIR:-/tmp}/nss-deploy.XXXXXX")"
  : > "$WORK/created"
  : > "$WORK/unread"
  : > "$WORK/ours"
  : > "$WORK/others"
  : > "$WORK/inside"
  NOTHING_DONE="Nothing was deleted."
  [ ! -t 0 ] || CAN_ASK="yes"

  read_record
  echo "Destroy of the Zscaler NSS deployment '$DEPLOYMENT_NAME'. Nothing is deleted before you confirm."

  local exists
  exists="$(azure group exists --name "$RESOURCE_GROUP" --output tsv)" ||
    die "Could not check whether the resource group '$RESOURCE_GROUP' exists. Nothing was deleted."
  if [ "$exists" = "true" ]; then
    GROUP_STATE="$(azure group show --name "$RESOURCE_GROUP" --query properties.provisioningState --output tsv)" ||
      die "Could not read the resource group '$RESOURCE_GROUP'. Nothing was deleted."
    collect_created subscription "$DEPLOYMENT_NAME" 0
    read_inventory ||
      die "The resources of the resource group '$RESOURCE_GROUP' cannot be listed. Nothing was deleted."
  elif [ "$exists" = "false" ]; then
    GROUP_STATE="gone"
  else
    die "Could not check whether the resource group '$RESOURCE_GROUP' exists. Nothing was deleted."
  fi
  prepare_zia
  decide_group
  destroy_summary
  confirm_destroy

  [ -z "$SERVER_ID" ] || delete_nss_server
  case "$GROUP_ACTION" in
    delete) delete_group ;;
    keep) delete_resources ;;
  esac
  STOP_NOTE="Stopped. Everything but the record of the deployment is deleted. Run this again to delete the record."
  azure deployment sub delete --name "$DEPLOYMENT_NAME" > /dev/null ||
    die "The record of the deployment '$DEPLOYMENT_NAME' was not deleted. The message above is from Azure. Everything else is deleted."
  STOP_NOTE=""
  say "The record of the deployment '$DEPLOYMENT_NAME' is deleted."
  echo
  echo "Destroyed."
  if [ -n "$SERVER_ID" ]; then
    echo "The deletion of the NSS server object is a pending change in ZIA until it is activated there."
  fi
  if [ "$GROUP_ACTION" = "keep" ]; then
    echo "The resource group '$RESOURCE_GROUP' is kept. 'az group delete --name $RESOURCE_GROUP' deletes it with everything in it."
  fi
}

trap cleanup EXIT
trap interrupted INT

case "$ACTION" in
  preview | deploy | watch | outputs | destroy | logo | "" | help | -h | --help)
    print_zscaler_logo
    ;;
esac

# Without a resource group, the values of a preview or a deployment are asked for in the
# terminal. Where nothing can be asked, the answers of an earlier run are used.
case "$ACTION" in
  preview | deploy)
    if [ -z "$RESOURCE_GROUP" ]; then
      if [ -t 0 ]; then
        QUESTIONS="yes"
        CAN_ASK="yes"
      elif [ -f "$ANSWERS_FILE" ]; then
        QUESTIONS="yes"
        ACCEPT_SAVED="yes"
      fi
    fi
    ;;
esac

case "$ACTION" in
  logo)
    exit 0
    ;;
  preview)
    prepare
    echo "Preview of the deployment. Nothing is created or changed."
    summary
    azure deployment sub what-if --name "$DEPLOYMENT_NAME" --location "$LOCATION" \
      --parameters "$PARAMETER_FILE" --parameters "${DEPLOY_ARGUMENTS[@]}"
    ;;
  deploy)
    prepare
    echo "Deployment of the Zscaler NSS VM."
    summary
    confirm
    azure deployment sub create --name "$DEPLOYMENT_NAME" --location "$LOCATION" \
      --parameters "$PARAMETER_FILE" --parameters "${DEPLOY_ARGUMENTS[@]}" --no-wait > /dev/null ||
      die "The deployment was not started. The message above is from Azure."
    say "The deployment '$DEPLOYMENT_NAME' was started."
    watch_deployment
    ;;
  watch)
    require_azure_cli
    require_deployment_name
    watch_deployment
    ;;
  outputs)
    require_azure_cli
    require_deployment_name
    show_outputs
    ;;
  destroy)
    require_azure_cli
    require_deployment_name
    destroy
    ;;
  "" | help | -h | --help)
    usage
    ;;
  *)
    usage >&2
    die "Unknown action '$ACTION'."
    ;;
esac
