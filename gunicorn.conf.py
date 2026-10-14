# Read by gunicorn from the working directory (deploy/fleet-portal.service).
#
# Gunicorn 26 opens a control socket under the user's home by default. The
# service runs with ProtectHome=read-only, so that fails and logs an error on
# every start. systemd already manages the process, so the socket is not
# needed. Older gunicorn versions ignore this setting.
control_socket_disable = True
