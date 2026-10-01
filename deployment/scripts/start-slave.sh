#!/bin/bash

source scripts/global-vars.sh

# Kill all children of this script when exiting
trap "$trap_exit_command" EXIT

tag=$1
master_ip=$2
public_ip=$3
private_ip=$4

# install dependency
# scp $ssh_options "scripts/cloud-deploy/user-script-slave.sh.template" "root@$public_ip:/root" || exit 6
# scp $ssh_options "scripts/cloud-deploy/global-vars.sh" "root@$public_ip:/root" || exit 7
# ssh $ssh_options root@$public_ip "chmod u+x /root/user-script-slave.sh.template;chmod u+x /root/global-vars.sh;/root/user-script-slave.sh.template"


init_command="
  cd $remote_work_dir/tls-data &&
  ./generate.sh -f $public_ip $private_ip &&
  mkdir -p $remote_work_dir/config"

slave_command="
  ulimit -Sn $open_files_limit &&
  export PATH=\$PATH:$remote_gopath/bin:$remote_work_dir/bin &&
  discoveryslave $tag $master_ip:$master_port $public_ip $private_ip"

echo "Setting up slave: $public_ip ($private_ip)"

# Periodically check slave status and wait until it is running.
slave_status=$(scripts/remote-machine-status.sh $public_ip)
echo "Slave status ($public_ip): $slave_status"
status_tries=0
while ! [[ "$slave_status" = "RUNNING" ]]; do
  status_tries=$((status_tries + 1))
  if [ "$status_tries" -ge 60 ]; then
    echo "Slave $public_ip did not report RUNNING." >&2
    exit 1
  fi
  sleep $machine_status_poll_period
  slave_status=$(scripts/remote-machine-status.sh $public_ip)
  echo "Slave status: $slave_status"
done

# Wait until master server is ready.
# This needs to happen before initialization of the slave, as the master needs to prepare files (e.g. code binaries)
# That the slave downloads during initialization.
echo "Waiting for master server."
ready_tries=0
while ! ssh $ssh_options -q -o "ConnectTimeout=10" "$remote_ssh_user@$master_ip" "cat $remote_ready_file > /dev/null"; do
  ready_tries=$((ready_tries + 1))
  if [ "$ready_tries" -ge 180 ]; then
    echo "Master did not become ready." >&2
    exit 1
  fi
  sleep $machine_status_poll_period
  echo "Master not ready. Retrying in $machine_status_poll_period seconds."
done

# The peer has no SSH key that can log into the master, so the controller pushes
# the compiled binaries and TLS material after the master has built them.
echo "Copying TLS data and binaries to $public_ip"
ssh $ssh_options "$remote_ssh_user@$public_ip" "mkdir -p $remote_gopath/bin $remote_work_dir/bin" || exit 1
rsync -rptz -e "ssh $ssh_options" "$remote_tls_directory" "$remote_ssh_user@$public_ip:$remote_work_dir/" || exit 1
rsync -rptz -e "ssh $ssh_options" "$remote_gopath/bin/" "$remote_ssh_user@$public_ip:$remote_gopath/bin/" || exit 1
node_key=${LADON_NODE_SSH_KEY:-$private_key_file}
scp $scp_options "$node_key" "$remote_ssh_user@$public_ip:$remote_private_key_file" || exit 1
scp $scp_options scripts/stubborn-scp.sh "$remote_ssh_user@$public_ip:$remote_work_dir/bin/stubborn-scp.sh" || exit 1
ssh $ssh_options "$remote_ssh_user@$public_ip" "chmod 600 $remote_private_key_file && chmod 755 $remote_work_dir/bin/stubborn-scp.sh" || exit 1

# Initialize slave.
# Retrying introduced because sometimes, when many instances of this script are run in parallel,
# The ssh command fails with "connection reset by peer" or similar error.
init_tries=0
while ! ssh $ssh_options $remote_ssh_user@$public_ip "$init_command"; do
  init_tries=$((init_tries + 1))
  if [ "$init_tries" -ge 5 ]; then
    echo "Failed to initialize slave $public_ip." >&2
    exit 1
  fi
  sleep 1
  echo "Retrying to initialize slave."
done

echo "Master ready. Starting slave process."
echo "ssh $ssh_options $remote_ssh_user@$public_ip \"$slave_command\""
ssh $ssh_options $remote_ssh_user@$public_ip "$slave_command"
