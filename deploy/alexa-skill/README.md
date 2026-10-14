# "Computer" Alexa skill (Echo requests via a relay, nothing exposed)

An Echo can only hand words to our code through an Alexa custom skill, and a skill's endpoint must be reachable from Amazon, but
this house is behind CGNAT. So the skill is hosted free by Amazon (Alexa-hosted, Node.js) and owns no logic: it seals each request
and posts it to a ntfy.sh topic; StoneSage (`StoneSage/backend/alexa_relay.py`, outbound connections only) reads it, runs Computer,
and posts the sealed answer to a second topic the skill is waiting on. Late answers (> ~5 s) are announced on the kitchen Echo.
Everything on the relay is sealed with one shared key (`config.json` -> `alexa_relay.key`, also in the generated `out/index.js`):
topic names and both cipher keys derive from it, so a stranger who finds the relay sees ciphertext and can answer nothing.

## Build the files (already done once; rerun after changing the phrase table or the key)
    python "deploy/alexa-skill/build_skill.py"
Writes `out/` (GITIGNORED: `index.js` contains the key): `index.js`, `package.json`, `interaction-model-computer.json`,
`interaction-model-attic-computer.json`. Never commit, paste into chat, or share `out/index.js`.

## Create the skill (developer.amazon.com/alexa/console/ask, the Amazon account the Echos use)
1. **Create Skill** -> name "Computer", locale English (US) -> experience type **Other** -> model **Custom** -> hosting **Alexa-hosted
   (Node.js)** -> template **Start from Scratch**.
2. **Build** tab -> **Interaction Model** -> **JSON Editor**: paste `out/interaction-model-computer.json`, **Save**, **Build Model**.
   Amazon's rules say a one-word invocation name must be a brand, so the console may refuse "computer". If it does, paste
   `out/interaction-model-attic-computer.json` instead (then you say "attic computer"). Not verified which one the console accepts.
3. **Code** tab: replace `index.js` with `out/index.js` and `package.json` with `out/package.json` (no dependencies), **Save**,
   **Deploy**. Wait for "Deployment successful".
4. **Test** tab -> set "Skill testing is enabled in" to **Development**. It then works on every Echo signed in to that Amazon account.
5. Try it on an Echo (read-only first): "Alexa, ask computer what time it is" / "Alexa, ask computer where is Rex".
   Orders: "Alexa, ask computer to turn off the porch light". After "Alexa, open computer" it listens for one sentence.

## What works and what does not
- Free text only arrives after a carrier word, and the skill is never told which sample matched, so every sentence opener is its
  own intent (`build_skill.py` COMMANDS / QUESTIONS: turn, set, is, where, what, who ... 80 intents). Add an opener there, rebuild,
  paste the model again. A sentence starting with another word gets "Start with turn, is, where, or what".
- Alexa's own speech engine does the listening (not our Parakeet + name correction), and the Echo answers in Alexa's voice.
- A reply that needs a yes/no (unlock, open the garage) keeps the session open: say "yes" or "no". The approval is also pushed to the
  phone, since an Echo session can end before the answer.
- Check it: `GET http://localhost:8888/api/alexa-relay/status` (requests, answered, announced, rejected, last_error, `users_seen`).
  `users_seen` holds a hash of the Alexa user id; put the owner's in `config.json alexa_relay.allowed_users` (a list) to refuse anyone else.
- Key leaked or lost: replace `alexa_relay.key` in both config.json files (local and the StoneSage host), rebuild, paste `index.js` again.

## Scenes by voice ("movie night") -- the step that is easy to miss
A bare "Alexa, movie night" never reaches a custom skill (Alexa only passes free text after "ask computer to ..."), and
Home Assistant's sentence-trigger automations are not heard by Alexa at all (they only see Assist, i.e. the app and Assist
satellites). To get the bare phrase to Computer, make an **Alexa Routine** per scene (Alexa app > More > Routines):
- When: **Voice** -> "movie night"; Action: **Skills** or **Custom** -> `ask computer to start movie night`
  (`start` and `run` are carrier words already in `build_skill.py` COMMANDS; the sentence after it must be a phrase in
  `config.json scenes.<name>.phrases`, or the scene name, and then Computer runs the scene without the model).
- Disable any other routine, smart-home group or HA-exposed script that is also named "movie night", or Alexa picks that one.
- "good night" is a built-in SIMPLE intent of the skill (a greeting, not the bedtime scene): use "start bedtime" in the routine.
Check the result in the trace (TRACE tab): kind courage, path reflex, tool run_scene.
