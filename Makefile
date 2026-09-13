.PHONY: all check install uninstall
all:
	$(MAKE) -C src/aw_input
check: all
	python3 -m unittest discover -s tests -v
	omarchy plugin validate plugin
install: all
	python3 install.py
uninstall:
	python3 install.py --remove
