class AlephError(Exception):
    """Base class for exceptions in this module."""
    pass


class PostTypeIsNoClassError(AlephError):
    """Exception raised when a received post_type is not resolvable to any python class in current runtime.
    """

    def __init__(self, content, message="Received post_type '{0}' from channel '{1}' does not currently exist as a class."):
        self.post_type = content['type']
        self.content = content['content']
        self.channel = content['channel']
        self.message = f"""{message.format(self.post_type, self.channel)}\n
        Response of {self.post_type} provides the following fields:\n
        {[key for key in self.content.keys()]}"""
        super().__init__(self.message)


class InvalidMessageTypeError(AlephError):
    """Exception raised when program received a different message type than expected.
    """

    def __init__(self, received, expected, message="Expected message type '{0}' but actually received '{1}'"):
        self.received = received
        self.expected = expected
        self.message = f"{message.format(self.expected, self.received)}"
        super().__init__(self.message)