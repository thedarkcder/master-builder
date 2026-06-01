FROM docker:27.5.1-cli-alpine3.21

RUN apk del openssh-client >/dev/null 2>&1 || true && \
    apk add --no-cache bash curl docker-cli-compose git jq openssh-server

COPY local_coolify_ssh_host_entrypoint.sh /usr/local/bin/local-coolify-ssh-host

RUN chmod +x /usr/local/bin/local-coolify-ssh-host && \
    ssh-keygen -A && \
    mkdir -p /root/.ssh && \
    chmod 700 /root/.ssh && \
    printf '%s\n' \
      'PermitRootLogin prohibit-password' \
      'PasswordAuthentication no' \
      'PubkeyAuthentication yes' \
      'AuthorizedKeysFile .ssh/authorized_keys' \
      > /etc/ssh/sshd_config.d/local-coolify.conf

EXPOSE 22

ENTRYPOINT ["/usr/local/bin/local-coolify-ssh-host"]
