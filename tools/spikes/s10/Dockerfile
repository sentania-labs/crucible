# Spike S10 publisher image: the codex worker base plus two shell scripts.
# Throwaway; never pushed. No credential of any kind is present in this image.
ARG BASE=crucible-worker:codex-0.153.4-fdfc3a7c883e
FROM ${BASE}
USER root
COPY --chmod=0755 publisher.sh cred-helper.sh /usr/local/lib/crucible-s10/
USER 1000:1000
ENTRYPOINT ["/usr/local/lib/crucible-s10/publisher.sh"]
