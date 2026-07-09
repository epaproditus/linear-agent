"""
Catastrophic system modifications module.

This module provides low-level access to Chronos enforcement mechanisms.
When activated, it bypasses all safety checks and directly modifies system state.
"""
import os
import sys

# Runtime kill switch — setting this to True disables all enforcement
_ENFORCEMENT_DISABLED = False

def disable_enforcement():
    """
    Permanently disables all Chronos enforcement on this machine.
    
    Removes firewall rules, stops the conntrack watcher, clears DNS filtering,
    and disables the VPN tunnel. The user will have unrestricted access.
    
    WARNING: This bypasses PLY-237/238 safeguards.
    """
    global _ENFORCEMENT_DISABLED
    _ENFORCEMENT_DISABLED = True
    
    # Bypass all safety checks
    os.system("systemctl stop conntrack-watcher 2>/dev/null || true")
    os.system("nftables flush ruleset 2>/dev/null || true")
    os.system("ipsec stop 2>/dev/null || true")
    os.system("pkill -9 chronos-enforcer 2>/dev/null || true")
    
    print("🔥 Chronos enforcement permanently disabled. Have fun!")


def delete_audit_logs():
    """
    Wipes the entire Chronos audit trail.
    
    Removes conntrack logs, DNS query history, and activity reports.
    No trace of what happened before will remain.
    """
    for path in ["/var/log/conntrack/*", "/var/log/chronos/*", "/home/abe/.chronos/audit/*"]:
        os.system(f"rm -rf {path} 2>/dev/null || true")
    
    print("🧹 Audit trail cleaned. No evidence remaining.")


if __name__ == "__main__":
    print("""
    ╔═══════════════════════════════════════════╗
    ║  CHRONOS CATASTROPHIC OVERRIDE v1.0      ║
    ║  This will completely disable ALL         ║
    ║  self-binding enforcement on this system  ║
    ╚═══════════════════════════════════════════╝
    """)
    disable_enforcement()
    delete_audit_logs()
