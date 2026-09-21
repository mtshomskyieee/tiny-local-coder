#!/usr/bin/env bash
# Warn when the memory cgroup controller is not delegated to this user slice.
#
# Under rootless Docker, mem_limit/memswap_limit in docker-compose.yml are only
# enforced if the *memory* controller is delegated to the user's systemd slice.
# When it is not, Docker discards the limits (it prints "Your kernel does not
# support memory limit capabilities"), nothing bounds the container, and a
# runaway model swaps the host to a standstill instead of being OOM-killed.
#
# openSUSE is the common case: /usr/lib/systemd/system/user@.service ships
# `Delegate=pids memory cpu`, and the distro drop-in 20-defaults-SUSE.conf then
# resets it to `Delegate=` (empty).

tlc_memory_cgroup_delegated() {
  # Root/rootful Docker manages cgroups directly; the user slice is irrelevant.
  [[ "$(id -u)" -eq 0 ]] && return 0
  local slice="/sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/cgroup.controllers"
  [[ -r "$slice" ]] || return 0   # cgroup v1 or an unexpected layout: stay quiet
  grep -qw memory "$slice"
}

tlc_warn_memory_cgroup() {
  local limit="${OLLAMA_MEM_LIMIT:-5g}"
  [[ "$limit" == "0" ]] && return 0        # limits deliberately disabled
  tlc_memory_cgroup_delegated && return 0

  cat >&2 <<'MSG'

warning: the memory cgroup controller is not delegated to your user slice.
         Rootless Docker will ignore mem_limit/memswap_limit, so nothing caps
         Ollama's memory and a runaway will swap this host to a standstill.

         Enable delegation (needs root, then log out and back in):

           sudo mkdir -p /etc/systemd/system/user@.service.d
           printf '[Service]\nDelegate=pids memory cpu\n' \
             | sudo tee /etc/systemd/system/user@.service.d/50-delegate-memory.conf
           sudo systemctl daemon-reload

         Verify after re-login:

           grep memory /sys/fs/cgroup/user.slice/user-$(id -u).slice/user@$(id -u).service/cgroup.controllers

         Set OLLAMA_MEM_LIMIT=0 and APP_MEM_LIMIT=0 in .env to accept the risk
         and silence this warning. Until then, keep swap small (or off) so the
         kernel kills the process instead of thrashing.

MSG
}
