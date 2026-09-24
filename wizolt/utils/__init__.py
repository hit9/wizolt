"""Self-contained readers: raw input in, plain facts out.

A module lives here when it imports nothing from the rest of wizolt -- only the standard library
-- and answers a question about raw input, such as what an image file's header says its size is,
or what object a model's malformed JSON plainly meant. It must not know about sessions, model
clients, or configuration; a module that needs those belongs in the layer that consumes it.
"""
