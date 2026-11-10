FROM python:3.13-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

# Google's adb: Debian's is too old for Android 11+ wireless debugging. Only the adb binary is kept.
ADD https://dl.google.com/android/repository/platform-tools_r37.0.1-linux.zip /tmp/platform-tools.zip
RUN python -c "import zipfile; zipfile.ZipFile('/tmp/platform-tools.zip').extract('platform-tools/adb', '/opt')" \
 && chmod +x /opt/platform-tools/adb && ln -s /opt/platform-tools/adb /usr/local/bin/adb && rm /tmp/platform-tools.zip

COPY android_backup /app/android_backup
COPY android-backup /usr/local/bin/android-backup
RUN chmod +x /usr/local/bin/android-backup

# HOME=/config keeps the adb key across recreates. PYTHONUTF8: phones have non-ASCII file names.
ENV PYTHONPATH=/app PYTHONUNBUFFERED=1 PYTHONUTF8=1 HOME=/config \
    BACKUP_DIR=/backups CONFIG_DIR=/config PORT=9872
VOLUME ["/backups", "/config"]
EXPOSE 9872
ENTRYPOINT ["tini", "--", "android-backup"]
CMD ["serve"]
