; Таблица сообщений журнала событий для источника FilePickerAPI.
; Текст каждого сообщения - %1: всю строку события передаёт сервис.
; Коды совпадают с EventId в app/event_tracking.py (это проверяет тест).
; Сами сообщения - только ASCII: mc читает файл в кодовой странице ANSI,
; а кириллицу этих комментариев копирует лишь в заголовок .h.

MessageIdTypedef=DWORD

LanguageNames=(Neutral=0x0000:MSG00000)

MessageId=1000
SymbolicName=SERVICE_STARTED
Language=Neutral
%1
.

MessageId=1001
SymbolicName=SERVICE_STOPPED
Language=Neutral
%1
.

MessageId=1002
SymbolicName=SERVICE_FAILED
Language=Neutral
%1
.

MessageId=1003
SymbolicName=BUILD_FOLDER_PERMISSIONS
Language=Neutral
%1
.

MessageId=1004
SymbolicName=LOG_FILE_UNAVAILABLE
Language=Neutral
%1
.

MessageId=1005
SymbolicName=ALREADY_RUNNING
Language=Neutral
%1
.

MessageId=1006
SymbolicName=OTHER_VERSION_RUNNING
Language=Neutral
%1
.

MessageId=2000
SymbolicName=DIRECTORY_UNAVAILABLE
Language=Neutral
%1
.

MessageId=2001
SymbolicName=DIRECTORY_RESTORED
Language=Neutral
%1
.

MessageId=2002
SymbolicName=DIRECTORY_STILL_UNAVAILABLE
Language=Neutral
%1
.

MessageId=2003
SymbolicName=DIRECTORY_SLOW
Language=Neutral
%1
.

MessageId=2004
SymbolicName=DIRECTORY_NOT_CREATED
Language=Neutral
%1
.

MessageId=3000
SymbolicName=FILE_ACCESS_ERROR
Language=Neutral
%1
.

MessageId=3001
SymbolicName=OTHER_WARNING
Language=Neutral
%1
.

MessageId=4000
SymbolicName=UNEXPECTED_ERROR
Language=Neutral
%1
.
