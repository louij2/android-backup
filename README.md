# android-backup

Backs up your Android phones and tablets to your server over ADB. No root and no app on the
device. Plug a device in and it's backed up, or give it the device's Wi-Fi address.

Any authorised device that's connected gets one backup every `BACKUP_INTERVAL_HOURS`
(default 24), so a drawer full of old phones only needs plugging in one at a time.

## What you get

`<backups>/<model>-<serial>/`

| Folder | Contents | Restore with |
|---|---|---|
| `files/` | Everything in shared storage: photos, downloads, documents, `Android/media`, `Android/data` | Copy back |
| `sd-<id>/` | The same for an SD card, if there is one | Copy back |
| `apks/<package>/<versionCode>/` | Every user-installed app, including split APKs | `adb install-multiple` |
| `snapshots/<date>/contacts.vcf` | Contacts, without photos | Any contacts app |
| `snapshots/<date>/sms.xml`, `calls.xml` | SMS text messages and call log | [SMS Backup & Restore](https://www.synctech.com.au/sms-backup-restore/) format |
| `snapshots/<date>/` | Package list, settings, `getprop` | Reference |

- **`files/` and `sd-<id>/` never delete.** A file deleted on the phone stays in the backup, so the folder
  only grows. A file changed on the phone overwrites the old copy.
- The last `KEEP_SNAPSHOTS` days of snapshots are kept (default 30).
- A file is copied again if its size or modification time has changed. Unplugging mid-run is
  safe, and the next run picks up where it left off.

## What it can't do

**Private app data can't be backed up without root.** That means logins, app databases and
chat histories kept inside apps. `adb backup` doesn't help: Android 12 and later exclude almost
every app from it. After a restore, apps come back but start logged out. Apps that keep their
own exports in shared storage are covered by `files/`. This can be most of a device: a tablet
with 209 GB used had 178 GB of private app data, whatever the apps keep privately.

MMS messages and contact photos aren't included. The backups aren't encrypted, so keep them on
a private share. `snapshots/` holds your messages and contacts in plain text.

## Run

```sh
docker run -d --name android-backup --restart unless-stopped -p 9872:9872 \
  --device-cgroup-rule='c 189:* rmw' -v /dev/bus/usb:/dev/bus/usb \
  -v /srv/android-backups:/backups \
  -v /srv/android-backup/config:/config \
  -e TZ=Europe/London \
  louij2/android-backup:latest
```

The device-cgroup rule and the `/dev/bus/usb` mount let devices be plugged in at any time
without `--privileged`.

## Add a device over USB

1. On the device, open Settings > About and tap **Build number** seven times.
2. In Developer options, turn on **USB debugging**.
3. Also turn on **Disable adb authorization timeout**, if it's there. Otherwise Android forgets
   the server after 7 days without a connection and asks again.
4. Plug it in and tap **Allow** with "Always allow" ticked. The status page shows when a device
   is waiting for this.

Check it worked with `docker exec android-backup android-backup adb devices -l`.

## Add a device over Wi-Fi

Authorise it over USB first, then:

```sh
docker exec android-backup android-backup adb -s <serial> tcpip 5555
```

Add `<device ip>:5555` to `ADB_TARGETS`. Android forgets `tcpip 5555` when it reboots, so repeat
that command after a reboot. Anyone on the network can then reach the device's ADB port, but they
still can't use it unless the device has authorised their key.

## Mobile data (light mode)

A device reached over the network that isn't on Wi-Fi, such as a tablet on Tailscale over LTE,
gets a light backup: new files up to `MOBILE_DATA_LIMIT_MB` (smallest first) and the contacts,
SMS and settings snapshot. App updates and bigger files wait. As soon as the device is back on
Wi-Fi or USB it gets a full backup, without waiting for the next interval.

App updates are most of the data: a phone with 51 apps averaged about 0.5 GB a day of updates,
while its changed files came to 30-50 MB.

## Settings

| Variable | Default | |
|---|---|---|
| `BACKUP_INTERVAL_HOURS` | `24` | Minimum time between good backups of a device |
| `RETRY_MINUTES` | `60` | Wait after a failed attempt |
| `ADB_TARGETS` | | Comma list of `host:port` to connect to |
| `SERIALS` | | Comma list; only back up these devices |
| `EXCLUDE` | caches, thumbnails, `Android/obb/` | Regex matched against paths relative to `/sdcard` or the SD card |
| `MOBILE_DATA_LIMIT_MB` | `500` | Most new-file data per backup on mobile data |
| `KEEP_SNAPSHOTS` | `30` | Dated snapshots to keep per device |
| `DISCORD_WEBHOOK_URL` | | Alert on failed backups |
| `NOTIFY_SUCCESS` | `false` | Also post successful backups |
| `PUID` / `PGID` | `99` / `100` | Owner of the finished backup |

## Status and metrics

- `http://server:9872/`: status page with a "Back up now" button per device
- `/metrics`: Prometheus, including `android_backup_last_success_timestamp_seconds`,
  `android_backup_last_ok`, `android_backup_files_failed` and `android_backup_safe_to_unplug`
- `POST /api/backup?serial=<serial>`: start a backup; leave out `serial` for every connected device

## Command line

```sh
docker exec android-backup android-backup backup [serial...]
docker exec android-backup android-backup status
docker exec android-backup android-backup adb devices -l
```

## License

MIT
