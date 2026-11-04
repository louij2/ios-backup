# ios-backup

Nightly iPhone/iPad backups to your server, over Wi-Fi. No Mac, no Windows VM.

It writes standard **Finder/iTunes (MobileBackup2)** backups, so **iMazing**, Finder or
iTunes can open them later, encrypted backups included. Under the hood it's
[pymobiledevice3](https://github.com/doronz88/pymobiledevice3).

- Pair once over USB. After that, every night it finds the device on your LAN (Bonjour
  `_apple-mobdev2`) and runs an incremental backup.
- If the device is plugged into the server it uses USB instead, which is quicker for the
  first full backup.
- A device that's asleep or away is retried every `RETRY_MINUTES` for `RETRY_WINDOW_HOURS`.
- Status page and Prometheus `/metrics` on port 9871. Optional Discord alert when a
  backup fails.

## Run

```sh
docker run -d --name ios-backup --network host --privileged --restart unless-stopped \
  -v /dev/bus/usb:/dev/bus/usb -v /run/udev:/run/udev:ro \
  -v /srv/ios-backups:/backups \
  -v /srv/ios-backup/config:/config \
  -v /srv/ios-backup/lockdown:/var/lib/lockdown \
  -e TZ=Europe/London -e BACKUP_TIMES=03:00 \
  louij2/ios-backup:latest
```

`--network host` is required for Bonjour discovery. `--privileged` and `/dev/bus/usb` are
only needed for USB, i.e. pairing and USB backups. Unraid users can install it from
[louij2/unraid-templates](https://github.com/louij2/unraid-templates).

## Pair a device (once)

1. Plug the iPhone into the server and unlock it.
2. `docker exec -it ios-backup ios-backup pair`, then tap **Trust** and enter the passcode.
   This also turns on Wi-Fi connections.
3. Optional but recommended: turn on **encrypted backups**. They also include saved
   passwords, Health and Wi-Fi networks:
   `docker exec -it ios-backup ios-backup pmd3 backup2 encryption on 'YOUR-BACKUP-PASSWORD'`
   Enter the passcode on the phone when it asks. Keep that password: you'll need it to
   open or restore the backup.
4. First backup now: `docker exec -it ios-backup ios-backup backup`. A full backup over
   USB takes about 1 to 2 hours per 70 GB.

Then unplug it. From now on it backs up over Wi-Fi at `BACKUP_TIMES`. Leave the phone on
Wi-Fi and, ideally, charging overnight. The phone may ask for its passcode when a backup
starts; that's iOS behaviour for encrypted backups.

> **Private Wi-Fi Address:** set it to **Fixed** (or Off) for your home network on the
> phone, under Settings → Wi-Fi → (i). A rotating address breaks Wi-Fi sync for every
> tool, Finder included.

## Open the backups with iMazing (Mac)

1. In Finder, connect to the share (Go → Connect to Server, e.g. `smb://tower/backups`).
2. In iMazing, open **Preferences → Backups** and add the backups folder (the one that
   holds the `<UDID>` folders) as a backup location.
3. The device's backup shows up in iMazing's backup list. Browse messages, photos and
   so on, or restore from it. Enter the backup password if it's encrypted.

You can also browse without iMazing:
`ios-backup pmd3 backup2 unback /backups/<UDID> --password ...` (on a copy).

## Configuration

| Variable | Default | |
|---|---|---|
| `BACKUP_TIMES` | `03:00` | Comma-separated `HH:MM`, container time zone (`TZ`) |
| `RETRY_MINUTES` | `20` | Retry interval while a device is unreachable |
| `RETRY_WINDOW_HOURS` | `5` | Stop retrying after this long |
| `BACKUP_TIMEOUT_HOURS` | `8` | Kill a stuck backup |
| `DISCORD_WEBHOOK_URL` | – | Alert on failures |
| `NOTIFY_SUCCESS` | `false` | Also post successes |
| `UDIDS` | – | Only back up these devices; the default is every paired device |
| `PORT` | `9871` | Status page + `/metrics` |
| `RUN_ON_START` | `false` | Run a round when the container starts |
| `USBMUXD` | `true` | `false` = Wi-Fi only |

Commands, all run with `docker exec ios-backup ios-backup ...`: `pair`, `backup [UDID]`,
`status`, and `pmd3 ...` (passthrough to pymobiledevice3).

Metrics: `ios_backup_last_success_timestamp_seconds`, `ios_backup_last_ok`,
`ios_backup_size_bytes`, `ios_backup_last_duration_seconds`, `ios_backup_running`,
all labelled `udid`/`name`.

## Security

`/var/lib/lockdown` holds the pair records, and they give full access to the paired
devices. Keep that folder private, and use encrypted backups.

## Licence

MIT
