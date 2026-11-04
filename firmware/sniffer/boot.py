import storage

# Mount the CIRCUITPY filesystem read-write so the firmware can write logs.
storage.remount("/", readonly=False)
