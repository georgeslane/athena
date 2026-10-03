# Rules

## Ground rules
- No pushing personal data or secrets or anything that could be used to compromise this project to github. Once this project is active on the Raspbery pi, it should not be possible for sensitive data to be pushed by the pi to github either.
- Everything should be tested, rather than assumed to work

## Atributes the application should have
- Lightweight. This means small system prompts and written efficiently and making good use of techniques like context cacheing where possible.
- Modular. It should be easy to switch out models and MCP servers at will
- Private. The assistent should have powers to interact with the outside world without making it possible to push sensitive data to anywhere not on my local network. Realistically this means things like only allowing push requests with my approval (and visibility of the content, etc)
- Capable. The assistant should be able to access and search folders on my mac and icloud (or google drive). It should be able to search MCP servers and reccomend new ones to me. It should be able to write skills telling it how to do certain things and it should be easy for me to review these, possibly through a web app or similar. It should be something I'll genuinally use.