"""Django integration. Add "deployangel.django" to INSTALLED_APPS; the app
config starts the agent, puts the middleware at the top of MIDDLEWARE, and
instruments Celery and RQ if they're installed."""
