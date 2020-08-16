# README

## Getting Started

* Install Ruby 2.5.8 using your favorite Ruby manager (asdf, rbenv, rvm)
* In your terminal, do the following:

```
bundle install
bundle exec jekyll build
bundle exec jekyll serve
```

## TBD

* Was looking into how to remove Google Calendar from the equation with regard to FullCalendar.  Currently, the Meetups calendar loads from a series of Google Calendars that load from Meetup ICS files. This line might work, but not far enough with it to know if its worthwhile.
```
Selene.parse(HTTParty.get('https://www.meetup.com/acm-chicago/events/ical/').body)
```
