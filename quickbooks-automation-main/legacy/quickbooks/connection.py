import win32com.client

def connect():
    session = win32com.client.Dispatch(
        "QBSessionManager.QBSessionManager"
    )

    session.OpenConnection(
        "",
        "Invoice Automation"
    )

    session.BeginSession(
        "",
        2
    )

    return session

session = connect()

print("Connected!")
