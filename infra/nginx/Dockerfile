FROM nginx:1.28-alpine

RUN apk add --no-cache openssl

# The stock entrypoint runs this before nginx starts.
COPY ensure-cert.sh /docker-entrypoint.d/05-ensure-cert.sh
RUN chmod +x /docker-entrypoint.d/05-ensure-cert.sh
