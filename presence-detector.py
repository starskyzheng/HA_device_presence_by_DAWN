#!/usr/bin/env python3
# pylint: disable=too-few-public-methods,invalid-name
# needed: python3 python3-yaml 

"""
A Wi-Fi device presence detector for Home Assistant that runs on OpenWRT
"""

import argparse
import json
import subprocess
import syslog
import time
from typing import Dict, Any, List, Callable, Optional
from urllib import request
from urllib.error import URLError, HTTPError


class Logger:
    """Class to handle logging to syslog"""

    def __init__(self, enable_debug: bool, log2stdout: bool=False) -> None:
        self.enable_debug = enable_debug
        self.log2stdout = log2stdout

    def log(self, text: str, is_debug: bool = False) -> None:
        """Log a line to syslog. Only log debug messages when debugging is enabled."""
        if is_debug and not self.enable_debug:
            return
        if self.log2stdout:
            print(text)
            return
        level = syslog.LOG_DEBUG if is_debug else syslog.LOG_INFO
        syslog.openlog(
            ident="presence-detector",
            facility=syslog.LOG_DAEMON,
            logoption=syslog.LOG_PID,
        )
        syslog.syslog(level, text)


class Settings:
    """Loads all settings from a JSON file and provides built-in defaults"""

    def __init__(self, config_file: str) -> None:
        self._settings = {
            "hass_url": "http://ha:8123",
            "do_not_track": [],
            "must_track": [],
            "only_track": [],
            "device_min_dawn_score": 0,
            "device_must_5g": False,
            "params": {},
            "offline_after": 3,
            "poll_interval": 15,
            "full_sync_polls": 10,
            "location": "home",
            "away": "not_home",
            "debug": False,
        }
        with open(config_file, "r", encoding="utf-8") as settings:
            self._settings.update(json.load(settings))

    def __getattr__(self, item: str) -> Any:
        return self._settings.get(item)


class PresenceDetector:
    """Presence detector that uses ubus polling to detect online devices"""

    def __init__(self, config_file: str, debug: bool = False, log2stdout: bool=False) -> None:
        self._settings = Settings(config_file)
        self._full_sync_counter = self._settings.full_sync_polls
        self._clients_seen: Dict[str, int] = {}
        debug = debug | self._settings.debug
        self._logger = Logger(debug, log2stdout)
        for mac in self._settings.must_track:
            self._clients_seen[mac] = 0

    @staticmethod
    def _post(url: str, data: dict, headers: dict):
        req = request.Request(
            url, data=json.dumps(data).encode("utf-8"), headers=headers
        )
        with request.urlopen(req, timeout=5) as response:
            return type(
                "", (), {"content": response.read(), "ok": response.code < 400}
            )()

    def _ha_seen(self, client: str, seen: bool = True) -> bool:
        """Call the HA device tracker 'see' service to update home/away status"""
        if seen:
            location = self._settings.location
        else:
            location = self._settings.away

        body = {"mac": client, "location_name": location, 
                "source_type": self._settings.source}
        if client in self._settings.params:
            body.update(self._settings.params[client])

        try:
            response = self._post(
                f"{self._settings.hass_url}/api/services/device_tracker/see",
                data=body,
                headers={"Authorization": f"Bearer {self._settings.hass_token}"},
            )
            self._logger.log(f"API Response: {response.content!r}", is_debug=True)
        #except (URLError, HTTPError) as ex:
        except Exception as ex:
            self._logger.log(str(ex))
            # Force full sync when HA returns
            self._full_sync_counter = 0
            return False
        if not response.ok:
            self._full_sync_counter = 0
        return response.ok

    def full_sync(self) -> None:
        """Syncs the state of all devices once every X polls"""
        self._full_sync_counter -= 1
        if self._full_sync_counter <= 0:
            sync_ok = True
            for client, offline_after in self._clients_seen.copy().items():
                if offline_after == self._settings.offline_after:
                    self._logger.log(f"full sync {client}", is_debug=True)
                    sync_ok &= self._ha_seen(client)
            # Reset timer only when all syncs were successful
            if sync_ok:
                self._full_sync_counter = self._settings.full_sync_polls

    def set_client_away(self, client: str) -> None:
        """Mark a client as away in HA"""
        self._logger.log(f"Device {client} is now away")
        if self._ha_seen(client, False):
            # Away call to HA was successful -> remove from list
            if client in self._clients_seen:
                del self._clients_seen[client]
        else:
            # Call failed -> retry next time
            self._clients_seen[client] = 1

    def set_client_home(self, client: str, ap_addr: str):
        """Mark a client as home in HA"""
        if client in self._settings.do_not_track:
            return
        if self._settings.only_track and client not in self._settings.only_track:
            return
        # Add ap prefix if ap_addr defined in settings
        #ap_loc = ""
        #if ap_addr in self._settings.ap2room:
        #    ap_loc = self._settings.ap2room[ap_addr]
        if client not in self._clients_seen:
            self._logger.log(f"Device {client} is now at {self._settings.location}")
            if self._ha_seen(client):
                self._clients_seen[client] = self._settings.offline_after
        else:
            self._clients_seen[client] = self._settings.offline_after

    def _get_all_online_clients(self) -> Dict[str, Any]:
        """Call ubus and get all online clients"""
        clients = {}
        process = subprocess.run(
            ["ubus", "call", "dawn", "get_hearing_map"],
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode != 0:
            self._logger.log(
                f"Error running ubus for `ubus call dawn get_hearing_map`: {process.stderr}"
            )
        response = json.loads(process.stdout)
        clients.update( response[self._settings.ssid] )
        return clients

    def _on_leave(self, client: str):
        """Callback for the Ubus watcher thread when a client leaves"""
        if self._settings.offline_after <= 1:
            self.set_client_away(client)


    @staticmethod
    def _get_ap_highest_score(json_now):
        ap_highest_score = -99
        ap_highest = ""
        for ap in json_now:
            if json_now[ap]["score"] > ap_highest_score:
                ap_highest_score = json_now[ap]["score"]
                ap_highest = ap
        return ap_highest, ap_highest_score
    
    def run(self) -> None:
        """Main loop for the presence detector"""

        # The main (sync) polling loop
        while True:
            seen_now = self._get_all_online_clients()
            # Periodically perform a full sync of all clients in case of connection failure
            self.full_sync()
            # Perform a regular 'changes only' sync with HA
            for client in seen_now:
                if(self._settings.device_must_5g):
                    if(seen_now[client]["channel_utilization"] < 15): continue
                ap_name, highest_dawn_score = self._get_ap_highest_score(seen_now[client])
                # First time showup must has high dawn score
                if(client in self._clients_seen or 
                    highest_dawn_score < self._settings.device_min_dawn_score):
                    continue
                self.set_client_home(client, ap_name)

            # Mark unseen clients as away after 'offline_after' intervals
            for client in self._clients_seen.copy():
                if client in seen_now:
                    continue
                self._clients_seen[client] -= 1
                if self._clients_seen[client] > 0:
                    continue
                # Client has not been seen x times, mark as away
                self.set_client_away(client)

            time.sleep(self._settings.poll_interval)

            self._logger.log(f"Clients seen: {self._clients_seen}", is_debug=True)

  


def main():
    """Main entrypoint: parse arguments and start all threads"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-c",
        "--config",
        help="Filename of configuration file",
        default="/etc/config/presence-detector.settings.json",
    )
    parser.add_argument( "--debug", action='store_true', default=False )
    parser.add_argument( "--log2stdout", action='store_true', default=False )
    args = parser.parse_args()
    detector = PresenceDetector(config_file=args.config, debug=args.debug, log2stdout=args.log2stdout)
    detector.run()



if __name__ == "__main__":
    main()
