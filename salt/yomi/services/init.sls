{% import 'macros.yml' as macros %}

{% set services = pillar.get('services', {}) %}

include:
  - .network
{% if pillar.get('salt-minion') %}
  - .salt-minion
{% endif %}

{% for service in services.get('enabled', []) %}
{{ macros.log('module', 'enable_service_' ~ service) }}
enable_service_{{ service }}:
  module.run:
    - service.enable:
      - name: {{ service }}
      - root: /mnt
    - unless: systemctl --root=/mnt --quiet is-enabled {{ service }} 2> /dev/null
{% endfor %}

{% for service in services.get('disabled', []) %}
{{ macros.log('module', 'disable_service_' ~ service) }}
disable_service_{{ service }}:
  module.run:
    - service.disable:
      - name: {{ service }}
      - root: /mnt
    - onlyif: systemctl --root=/mnt --quiet is-enabled {{ service }} 2> /dev/null
{% endfor %}
