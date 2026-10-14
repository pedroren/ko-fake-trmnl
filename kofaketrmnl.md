# ko-fake-trmnl

## Description
Service/api that can can emulate a TRMNL BYOS so that clients like TRMNL KOReader plugin (https://github.com/usetrmnl/trmnl-koreader) can connect to it.

Returning the required json for a TRMNL client:
```
{ "image_url": "http://your-server-ip/dashboard.png", "refresh_rate": 60 }
```

## Definition
The service will suport multiple playlists, those playlist will just contain a list of image_url and refresh_rate.

Playlist will be accesible by the client via the api url like:
http://your-server-ip:5000/playlist/playlist1 or http://your-server-ip:5000/playlist/playlist2

Current item will rotate after the refresh_rate is done. But to be more efficient, items could iterate on each request, playlist arent supposed to be shared by multiple clients. Optionally, we could create devices and assign playlist, so we can keep separate playlist states for each device, in that case the client url would be http://your-server-ip:5000/device/deviceid1.

The service needs a CRUD frontend to manage the playlists, and persist the data into json files, for simplicity. Enable support for persisting the file ouside a docker container.

Suggest platform/stack. Maybe Python. Suggest the most friendly and lightweight.