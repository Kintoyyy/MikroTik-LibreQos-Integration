/user group add name=API_READ policy="read,sensitive,api,!policy,!local,!telnet,!ssh,!ftp,!reboot,!write,!test,!winbox,!password,!web,!sniff,!romon"

/user add name="libreQos_API" group=API_READ password="<Strong Password>" address="<LibreQos IP Address>" disabled=no;