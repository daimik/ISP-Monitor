#!/bin/sh
# Run the speedtest monitor in the background with auto-restart loop.
# The dashboard runs in the foreground — if it dies, Docker restarts the container.

(while true; do
    python speedtest_monitor.py
    echo "Monitor exited (code $?), restarting in 10s..."
    sleep 10
done) &

exec python dashboard.py
