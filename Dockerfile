# Python 3.13+: iOS 18.2+ only allows TCP RemotePairing tunnels, which need TLS-PSK from 3.13's ssl module.
FROM python:3.13-slim

# usbmuxd: only needed for USB (pairing, fast first backup); Wi-Fi backups go through a RemotePairing tunnel.
RUN apt-get update \
 && apt-get install -y --no-install-recommends usbmuxd tini \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY ios_backup /app/ios_backup
COPY ios-backup /usr/local/bin/ios-backup
RUN chmod +x /usr/local/bin/ios-backup

ENV PYTHONPATH=/app PYTHONUNBUFFERED=1 HOME=/config \
    BACKUP_DIR=/backups CONFIG_DIR=/config BACKUP_TIMES=03:00 PORT=9871
VOLUME ["/backups", "/config", "/var/lib/lockdown"]
EXPOSE 9871
ENTRYPOINT ["tini", "--", "ios-backup"]
CMD ["serve"]
