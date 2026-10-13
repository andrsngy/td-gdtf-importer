GDTF RIG - TouchDesigner component
==================================
MIT License Copyright (c) 2026 andrasnagy.xyz  (full text: LICENSE)

Contents
--------
  gdtf_rig.tox        the rig (5 GDTF fixtures, DMX out, show control, fixture labels)
  LICENSE             MIT License (also inside the tox: gdtf_rig/LICENSE)
  gdtf/               everything the rig loads, found relative to the tox
    *.py              extension code (also stored inside the tox; these files are kept in sync)
    shaders/          beam / gobo GLSL
    library/          GDTF fixture profiles (.gdtf)
    cache/            extracted 3D models + parsed profiles of the fixtures in the rig
  snapshots/          labeled rig snapshots are saved here

Keep this folder structure together. Move or copy the whole folder, not just the .tox.

Using it
--------
Option A (recommended): External .tox
  1. Create a Base COMP, or drop gdtf_rig.tox into your network.
  2. On the rig's Common page, set "External .tox" to this folder's gdtf_rig.tox.
  The rig's Package > Root Folder then points at this folder automatically.

Option B: put this folder's contents next to your .toe
  Copy gdtf_rig.tox, gdtf/ and snapshots/ into the folder of your .toe, then drag
  gdtf_rig.tox in. Root Folder defaults to the project folder, so it finds gdtf/.

Option C: anywhere
  Drag gdtf_rig.tox in and type this folder's path into Package > Root Folder.

Getting fixtures from GDTF Share (online)
-----------------------------------------
GDTF Share (https://gdtf-share.com) is the free online library of GDTF fixture profiles
made by the manufacturers. The rig can search it and download profiles directly.

  1. Create a free account at https://gdtf-share.com.
  2. Select a fixture in the rig (e.g. gdtf_rig/fixture1) and open its GDTF page.
  3. Set Source to "share".
  4. Log in, in one of two ways:
       - type your account into Share User and Share Password, or
       - (safer) set the environment variables GDTF_SHARE_USER and GDTF_SHARE_PASSWORD
         before starting TouchDesigner and leave both fields empty.
  5. Press Refresh List. The full fixture catalogue is downloaded (this takes a moment);
     Status shows how many profiles were found.
  6. Narrow it down with Search All, Find Manufacturer and Find Fixture
     (Latest Revision Only hides older versions; Matches shows how many are left).
  7. Pick the Manufacturer and the Fixture from the menus.
  8. Press Load Fixture. The .gdtf file is downloaded into gdtf/library/, its 3D models
     are extracted into gdtf/cache/, and the fixture is rebuilt with the new profile.
  9. Choose the DMX Mode, then set Universe and Address on the DMX page
     (Next Free shows the first free address after this fixture).

Once downloaded, a profile stays in gdtf/library/ and works offline:
  - Source "folder" lists every .gdtf file in the Library Folder (no login needed).
  - Or pick a single file in GDTF File and press Load File.

To add more fixtures, copy an existing fixture COMP inside gdtf_rig (Ctrl+C / Ctrl+V)
and load a different profile into it. It is picked up by the render, the DMX output and
the labels automatically. Give it its own Fixture ID and address.

Important: Share User / Share Password are saved in your .toe / .tox. Clear them before
you share your project, or use the environment variables instead.

Fixture labels / printable plot
-------------------------------
  gdtf_rig/fixture_labels  (Base COMP)
    Labels page   : Show Fixture Labels, ID, Universe/Address, Name, Mode, Patch Legend,
                    Label Size, Label Offset, Auto-Number IDs
    Snapshot page : Save Labeled Snapshot  -> PNG in snapshots/
  Each fixture has a "Fixture ID" parameter on its DMX page.

License
-------
MIT License Copyright (c) 2026 andrasnagy.xyz

Covers the rig, its code (gdtf/*.py) and shaders (gdtf/shaders/). Full text: LICENSE.

Third-party content: the GDTF fixture profiles in gdtf/library/ and the 3D models and
images extracted from them into gdtf/cache/ are made by their manufacturers / GDTF Share
contributors. They are not covered by the MIT License and remain under their own terms.

Note: the DMX output (gdtf_rig/dmx_out) is active and sends Art-Net / DMX as soon as
the rig is loaded. Check its settings before loading it on a show network.
