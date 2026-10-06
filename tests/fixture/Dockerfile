FROM busybox:1.37
ARG GIT_COMMIT
ARG GIT_DIRTY
LABEL org.opencontainers.image.revision=$GIT_COMMIT
LABEL org.opencontainers.image.dirty=$GIT_DIRTY
CMD ["sh", "-c", "echo fixture-passed"]
