ARG PHP_IMAGE=php:8.1-apache-bookworm
FROM ${PHP_IMAGE} AS base
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg tar gzip \
    && docker-php-ext-install pdo_mysql mysqli \
    && cp "$PHP_INI_DIR/php.ini-production" "$PHP_INI_DIR/php.ini"
COPY common/health.php /var/www/html/health.php

FROM base AS api
COPY api/ /var/www/html/

FROM base AS web
RUN a2enmod proxy proxy_http
COPY apache/lab-proxy.conf /etc/apache2/conf-enabled/lab-proxy.conf
COPY web/ /var/www/html/
