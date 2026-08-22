Goal: 
---

The goal of this project is to allow a multi-player lobby to control a robot or robots within an arena. 

Users can create their own lobbies and using a key, set up a bidirectional webrtc stream allowing them to connect their robots to allow it to be controlled with basic controls over the web with a UI that works for mobile or desktop.  

Part of the goal is to allow "Malkovich mode" where multiple users can jointly control the robot while in a voice lobby, allowing joint control.  One feature that has been implemented along these lines is that when multiple people are in the lobby, all must approve for the voice chat audio to be routed to the robot speaker.  

Currently the robot I've connected is a Yahboom Rosmaster A1.  There's a relay which can be set up to communicate with the server so multiple robots can theoretically be in the lobby.  

For the sake of making it easier to test, I have also begun setting up a simulation game that runs over webrtc to test the API <-> client hop. 
