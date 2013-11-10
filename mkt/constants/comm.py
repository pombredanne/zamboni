# Number of days a token is valid for.
THREAD_TOKEN_EXPIRY = 30

# Number of times a token can be used.
MAX_TOKEN_USE_COUNT = 5

NO_ACTION = 0
APPROVAL = 1
REJECTION = 2
DISABLED = 3
MORE_INFO_REQUIRED = 4
ESCALATION = 5
REVIEWER_COMMENT = 6
RESUBMISSION = 7

NOTE_TYPES = [
    NO_ACTION,
    APPROVAL,
    REJECTION,
    DISABLED,
    MORE_INFO_REQUIRED,
    ESCALATION,
    REVIEWER_COMMENT,
    RESUBMISSION
]

# Prefix of the reply to address in comm emails.
REPLY_TO_PREFIX = 'reply+'
